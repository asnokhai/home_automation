"""
Bring! shopping list access for the voice assistant.

Named for the domain rather than the vendor, for the same reason
trello_board.py is not trello.py: main.py runs from inside src/, which puts
src/ first on sys.path, so a module called bring.py or bring_api.py here would
shadow the package it imports.

Unlike TrelloBoard, nothing here goes through asyncio.to_thread. bring-api is
aiohttp-native, so its calls already yield to the event loop and the wake word
keeps being scored while a request is in flight.

Public methods return a speakable sentence rather than raising, so a failure is
heard instead of vanishing into run_action's exception handler.
"""

from __future__ import annotations

import asyncio
import os

import aiohttp
from bring_api import (
    Bring,
    BringAuthException,
    BringParseException,
    BringRequestException,
)
from dotenv import load_dotenv

load_dotenv()

# A voice turn that hangs is worse than one that fails: aiohttp's own default
# would hold the tool call open for five minutes with the assistant silent.
REQUEST_TIMEOUT = 15

# Long lists are unspeakable; read the first few and say how many remain.
SPEAK_LIMIT = 7


class ShoppingListError(Exception):
    """Something the user should hear about, phrased for speech."""


class ShoppingList:
    def __init__(self):
        self._email = os.getenv("BRING_EMAIL", "").strip()
        self._password = os.getenv("BRING_PASSWORD", "").strip()
        self._list_name = os.getenv("BRING_LIST_NAME", "").strip()
        self._list_uuid = os.getenv("BRING_LIST_UUID", "").strip()

        # All built on first use, never here. Bring.__init__ calls
        # asyncio.get_running_loop(), and an aiohttp session belongs to the
        # loop that made it, so both have to wait for a loop to exist.
        self._session = None
        self._bring = None
        self._loop = None
        self._lock = None
        self._logged_in = False

    # -- actions ---------------------------------------------------------

    async def add_item(self, item_name, quantity=None):
        """Put one item on the shopping list, optionally with an amount."""
        return await self._run(self._add_item, item_name, quantity)

    async def list_items(self):
        """Read back everything still to buy."""
        return await self._run(self._list_items)

    # -- action implementations ------------------------------------------

    async def _add_item(self, item_name, quantity=None):
        name = _clean(item_name)
        if not name:
            raise ShoppingListError("What should I add to the shopping list?")

        # Bring matches the name against its product catalogue to pick an icon
        # and an aisle, so the amount has to travel separately -- "two litres
        # of milk" as a name would become a bespoke entry that never merges
        # with the Milk tile already on the list.
        spec = _clean(quantity)

        bring = await self._connect()
        await bring.save_item(await self._resolve_list_uuid(), name, spec)

        if spec:
            return f"Added {name}, {spec}, to the shopping list"
        return f"Added {name} to the shopping list"

    async def _list_items(self):
        bring = await self._connect()
        items = (await bring.get_list(await self._resolve_list_uuid())).items.purchase

        # .recently is the already-bought pile; reading it back would be wrong.
        if not items:
            return "Nothing on the shopping list"

        return f"{_count(len(items), 'item')} on the shopping list. " + _speak_items(items)

    # -- internals -------------------------------------------------------

    async def _run(self, fn, *args):
        """Run an implementation, speaking any failure instead of raising."""
        try:
            try:
                return await fn(*args)
            except BringAuthException:
                # bring-api refreshes its own access token, so reaching here
                # means the refresh token is gone too -- sign in from scratch
                # and try once more. The failed call never reached the server,
                # so a retry cannot add the same item twice.
                print("  Bring: signing in again")
                self._logged_in = False
                return await fn(*args)

        except ShoppingListError as e:
            print(f"  Bring: {e}")
            return str(e)
        except BringAuthException as e:
            print(f"  Bring auth error: {e}")
            return "I could not sign in to Bring, check the login details"
        except (BringRequestException, BringParseException, aiohttp.ClientError,
                asyncio.TimeoutError) as e:
            print(f"  Bring error: {e}")
            return "I could not reach Bring"
        except Exception as e:
            print(f"  Bring error: {e}")
            return "I could not reach Bring"

    async def _connect(self):
        """Session, client and login on first use. Safe to call every time."""
        # No await between the test and the assignment, so two voice turns
        # landing together cannot each make their own lock.
        if self._lock is None:
            self._lock = asyncio.Lock()

        async with self._lock:
            if not self._email:
                raise ShoppingListError("Bring is not set up, the email address is missing")
            if not self._password:
                raise ShoppingListError("Bring is not set up, the password is missing")

            loop = asyncio.get_running_loop()
            if self._session is not None and self._loop is not loop:
                # A second asyncio.run in the same process -- a diagnostic in
                # test/, usually. The old session is wired to a dead selector
                # and cannot be awaited closed from here, so drop it.
                self._session = self._bring = None
                self._logged_in = False

            if self._session is None or self._session.closed:
                self._session = aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT))
                self._loop = loop
                self._bring = Bring(self._session, self._email, self._password)
                self._logged_in = False

            if not self._logged_in:
                await self._bring.login()
                self._logged_in = True

            return self._bring

    async def _resolve_list_uuid(self):
        """The uuid of the list to use, looked up once and remembered."""
        if self._list_uuid:
            return self._list_uuid

        bring = await self._connect()
        lists = (await bring.load_lists()).lists
        if not lists:
            raise ShoppingListError("There are no lists on that Bring account")

        if not self._list_name:
            # Most accounts have exactly one list; BRING_LIST_NAME is there
            # for the rest.
            self._list_uuid = lists[0].listUuid
            return self._list_uuid

        target = _match_by_name(lists, self._list_name)
        if target is None:
            raise ShoppingListError(
                f"I could not find a Bring list called {self._list_name}")

        # Only the uuid is remembered -- items are always fetched fresh.
        self._list_uuid = target.listUuid
        return self._list_uuid

    async def aclose(self):
        """Close the session. Safe before anything opened, and safe twice."""
        session, self._session, self._bring = self._session, None, None
        self._logged_in = False
        if session is not None and not session.closed:
            await session.close()


# -- matching and speech helpers ----------------------------------------


def _clean(text):
    return str(text or "").strip()


def _match_by_name(items, wanted):
    """Best match on `.name`, or None.

    Simpler than trello_board's matcher on purpose: this name comes from .env,
    typed once by hand, not from Whisper.
    """
    wanted = _clean(wanted).lower()
    names = [(_clean(item.name).lower(), item) for item in items]

    for name, item in names:
        if name == wanted:
            return item

    contained = [item for name, item in names if name and (wanted in name or name in wanted)]
    if contained:
        return min(contained, key=lambda item: len(item.name))

    return None


def _count(n, noun):
    return f"{n} {noun}" + ("" if n == 1 else "s")


def _speak_items(items):
    """Item names as a sentence, truncated so a long list stays listenable."""
    said = []
    for item in items[:SPEAK_LIMIT]:
        # itemId is the display name -- bring-api has already translated it
        # into the list's language by the time get_list returns.
        said.append(f"{item.itemId}, {item.specification}"
                    if item.specification else item.itemId)

    rest = len(items) - len(said)
    return ". ".join(said) + (f". And {rest} more" if rest > 0 else "")


if __name__ == "__main__":
    # Setup helper: run this to find the list uuid for .env, and to check that
    # the login actually works, without needing the rest of the Pi. It only
    # reads -- running it repeatedly changes nothing.
    import sys

    shopping = ShoppingList()

    if not shopping._email or not shopping._password:
        print("BRING_EMAIL and BRING_PASSWORD must both be in .env")
        sys.exit(1)

    async def _setup():
        try:
            bring = await shopping._connect()
            print(f"Signed in as {shopping._email}\n")

            print("Lists:")
            for lst in (await bring.load_lists()).lists:
                print(f"  {lst.listUuid}  {lst.name}")

            uuid = await shopping._resolve_list_uuid()
            print(f"\nUsing {uuid}")
            print("Put that in .env as BRING_LIST_UUID to skip this lookup,")
            print("or set BRING_LIST_NAME if you would rather name the list.\n")

            items = (await bring.get_list(uuid)).items.purchase
            print(f"To buy ({len(items)}):")
            for item in items:
                print(f"  {item.itemId}" + (f"  [{item.specification}]"
                                            if item.specification else ""))
        finally:
            await shopping.aclose()

    try:
        asyncio.run(_setup())
    except ShoppingListError as e:
        print(e)
        sys.exit(1)
    except BringAuthException as e:
        print(f"Sign in failed: {e}")
        sys.exit(1)
