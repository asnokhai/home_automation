"""
Trello board access for the voice assistant.

Deliberately NOT named trello.py: main.py runs from inside src/, which puts
src/ first on sys.path, so a module by that name would shadow the py-trello
package it imports.

Every public method is async and does its network work in a worker thread.
py-trello is synchronous requests underneath, and the voice assistant awaits
actions on the event loop -- a blocking call here would deafen the wake word
for the whole round trip.

Public methods return a speakable sentence rather than raising, so a failure
is heard instead of vanishing into run_action's exception handler.
"""

from __future__ import annotations

import asyncio
import difflib
import os
from datetime import datetime, time as dtime, timedelta

from dateutil import parser as dateparser
from dotenv import load_dotenv
from trello import TrelloClient

load_dotenv()

AUTHORIZE_URL = (
    "https://trello.com/1/authorize?expiration=never&scope=read,write"
    "&response_type=token&name=HomeAutomation&key={key}"
)

# Below this, difflib's best guess is worse than admitting we did not find it.
MATCH_CUTOFF = 0.6

# Long card lists are unspeakable; read the first few and say how many remain.
SPEAK_LIMIT = 7


class TrelloError(Exception):
    """Something the user should hear about, phrased for speech."""


class TrelloBoard:
    def __init__(self):
        self._api_key = os.getenv("TRELLO_API_KEY", "").strip()
        self._api_secret = os.getenv("TRELLO_API_SECRET", "").strip()
        self._token = os.getenv("TRELLO_TOKEN", "").strip()
        self._token_secret = os.getenv("TRELLO_TOKEN_SECRET", "").strip()

        self._board_id = os.getenv("TRELLO_BOARD_ID", "").strip()
        self._board_name = os.getenv("TRELLO_BOARD_NAME", "").strip()
        self._default_list = os.getenv("TRELLO_DEFAULT_LIST", "").strip()
        self._done_list = os.getenv("TRELLO_DONE_LIST", "Done").strip()

        self._client = None

    # -- actions ---------------------------------------------------------

    async def create_task(self, task_name, list_name=None, due=None):
        """Add a card, optionally to a named list and with a due date."""
        return await self._run(self._create_task, task_name, list_name, due)

    async def mark_as_completed(self, task_name):
        """Move a card to the done list and tick its due date."""
        return await self._run(self._mark_as_completed, task_name)

    async def archive(self, task_name):
        """Archive a card, taking it off the board."""
        return await self._run(self._archive, task_name)

    async def list_tasks(self, list_name=None):
        """Read back the open cards on one list, or on the whole board."""
        return await self._run(self._list_tasks, list_name)

    async def set_due_date(self, task_name, due):
        """Put a due date on an existing card."""
        return await self._run(self._set_due_date, task_name, due)

    async def list_due_tasks(self, days=None):
        """Read back what is due, by default today and anything overdue."""
        return await self._run(self._list_due_tasks, days)

    # -- action implementations, all synchronous -------------------------

    def _create_task(self, task_name, list_name=None, due=None):
        name = _clean(task_name)
        if not name:
            raise TrelloError("A task needs a name")

        target = self._resolve_list(list_name)
        deadline = _parse_due(due)

        card = target.add_card(name)
        if deadline:
            card.set_due(deadline)
            return f"Added {name} to {target.name}, due {_speak_date(deadline)}"

        return f"Added {name} to {target.name}"

    def _mark_as_completed(self, task_name):
        card, board = self._find_card(task_name)

        done = _match_by_name(board.open_lists(), self._done_list)
        if done is None:
            raise TrelloError(
                f"I found {card.name} but there is no {self._done_list} list on the board")

        # Both halves of "completed": off the working list, and the due-date
        # checkmark ticked so it stops showing up as outstanding.
        if card.list_id != done.id:
            card.change_list(done.id)
        if getattr(card, "due", None):
            card.set_due_complete()

        return f"Marked {card.name} as complete"

    def _archive(self, task_name):
        card, _ = self._find_card(task_name)
        card.set_closed(True)
        return f"Archived {card.name}"

    def _list_tasks(self, list_name=None):
        board = self._board()

        if _clean(list_name):
            target = self._resolve_list(list_name, board=board)
            cards = target.list_cards()
            where = target.name
        else:
            cards = board.open_cards()
            where = "the board"

        if not cards:
            return f"Nothing on {where}"

        return f"{_count(len(cards), 'task')} on {where}. " + _speak_names(cards)

    def _set_due_date(self, task_name, due):
        deadline = _parse_due(due)
        if not deadline:
            raise TrelloError("I did not catch a date for that")

        card, _ = self._find_card(task_name)
        card.set_due(deadline)
        return f"{card.name} is due {_speak_date(deadline)}"

    def _list_due_tasks(self, days=None):
        try:
            horizon = 0 if days in (None, "") else int(float(days))
        except (TypeError, ValueError):
            horizon = 0
        horizon = max(0, horizon)

        cutoff = datetime.combine(
            datetime.now().date() + timedelta(days=horizon), dtime.max).astimezone()

        due = []
        for card in self._board().open_cards():
            when = _card_due(card)
            # Trello keeps the due date on a card after it is ticked complete;
            # those are finished, not outstanding.
            if when and when <= cutoff and not getattr(card, "is_due_complete", False):
                due.append((when, card))

        if not due:
            if horizon == 0:
                return "Nothing due today"
            return f"Nothing due in the next {_count(horizon, 'day')}"

        due.sort(key=lambda pair: pair[0])
        cards = [card for _, card in due]
        if horizon == 0:
            window = "due today or overdue"
        else:
            window = f"due in the next {_count(horizon, 'day')}"

        return f"{_count(len(cards), 'task')} {window}. " + _speak_names(cards)

    # -- internals -------------------------------------------------------

    async def _run(self, fn, *args):
        """Run a sync implementation off the loop, speaking any failure."""
        try:
            return await asyncio.to_thread(fn, *args)
        except TrelloError as e:
            print(f"  Trello: {e}")
            return str(e)
        except Exception as e:
            print(f"  Trello error: {e}")
            return "I could not reach Trello"

    def _connect(self):
        """Build the client on first use. No network happens here."""
        if self._client is not None:
            return self._client

        if not self._api_key:
            raise TrelloError("Trello is not set up, the API key is missing")
        if not self._token:
            raise TrelloError("Trello is not set up, the token is missing")

        if self._token_secret:
            self._client = TrelloClient(
                api_key=self._api_key, api_secret=self._api_secret,
                token=self._token, token_secret=self._token_secret)
        else:
            # With no OAuth token py-trello signs with plain ?key=&token=
            # query params -- and reads that token out of the api_secret slot.
            # See TrelloClient.fetch_json. Passing the real secret here would
            # authenticate as nobody.
            self._client = TrelloClient(api_key=self._api_key, api_secret=self._token)

        return self._client

    def _board(self):
        """Fetch the configured board, resolving a board name once."""
        client = self._connect()

        if not self._board_id:
            if not self._board_name:
                raise TrelloError("Trello is not set up, no board is configured")

            board = _match_by_name(client.list_boards(), self._board_name)
            if board is None:
                raise TrelloError(f"I could not find a board called {self._board_name}")

            # Only the id is remembered -- lists and cards are always fetched
            # fresh, so the board never goes stale under us.
            self._board_id = board.id
            return board

        return client.get_board(self._board_id)

    def _resolve_list(self, list_name, board=None):
        """A List by spoken name, falling back to the configured default."""
        board = board or self._board()
        lists = board.open_lists()
        if not lists:
            raise TrelloError("That board has no lists")

        spoken = _clean(list_name)
        wanted = spoken or self._default_list
        if not wanted:
            return lists[0]

        target = _match_by_name(lists, wanted)
        if target is None:
            if spoken:
                raise TrelloError(f"I could not find a list called {spoken}")
            return lists[0]

        return target

    def _find_card(self, task_name):
        """The open card best matching a spoken name, plus its board."""
        wanted = _clean(task_name)
        if not wanted:
            raise TrelloError("Which task did you mean?")

        board = self._board()
        cards = board.open_cards()
        if not cards:
            raise TrelloError("There are no tasks on the board")

        card = _match_by_name(cards, wanted)
        if card is None:
            raise TrelloError(f"I could not find a task called {wanted}")

        return card, board


# -- matching and speech helpers ----------------------------------------


def _clean(text):
    return str(text or "").strip()


def _match_by_name(items, wanted):
    """Best fuzzy match on `.name`, or None if nothing is close enough.

    Whisper rarely returns a card's name verbatim, so exact matching alone
    would fail on most spoken commands. Tried in order: exact, then
    containment either way, then difflib's ratio.
    """
    wanted = _clean(wanted).lower()
    if not wanted:
        return None

    names = [(_clean(getattr(item, "name", "")).lower(), item) for item in items]

    for name, item in names:
        if name == wanted:
            return item

    contained = [item for name, item in names if name and (wanted in name or name in wanted)]
    if contained:
        # Shortest wins: "milk" should match "Buy milk", not "Buy milk and bread".
        return min(contained, key=lambda item: len(item.name))

    scored = [(difflib.SequenceMatcher(None, wanted, name).ratio(), item)
              for name, item in names if name]
    if not scored:
        return None

    ratio, item = max(scored, key=lambda pair: pair[0])
    return item if ratio >= MATCH_CUTOFF else None


def _parse_due(due):
    """Turn whatever the model sent into an aware datetime, or None.

    The model is asked for ISO 8601, but 'tomorrow' and 'friday' still turn up
    and dateutil handles those. A bare date becomes 9am local rather than
    midnight, since midnight reads as the day before on a Trello card.
    """
    text = _clean(due)
    if not text or text.lower() in ("none", "null", "no", "unknown"):
        return None

    now = datetime.now()
    lowered = text.lower()
    if lowered == "today":
        parsed = now
    elif lowered == "tomorrow":
        parsed = now + timedelta(days=1)
    else:
        default = now.replace(hour=9, minute=0, second=0, microsecond=0)
        try:
            parsed = dateparser.parse(text, default=default)
        except (ValueError, OverflowError, TypeError):
            return None

    if parsed is None:
        return None

    # dateutil resolves a bare weekday within the current week, so "friday"
    # said on a Saturday lands two days ago. Only word-shaped input is rolled
    # forward -- a spoken date with digits in it is taken at face value.
    if parsed < now and not any(ch.isdigit() for ch in text):
        parsed += timedelta(days=7)

    if parsed.tzinfo is None:
        parsed = parsed.astimezone()

    return parsed


def _card_due(card):
    """A card's due date as an aware datetime, or None."""
    raw = getattr(card, "due", None)
    if not raw:
        return None

    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.astimezone()

    try:
        parsed = dateparser.parse(raw)
    except (ValueError, OverflowError, TypeError):
        return None

    if parsed is None:
        return None

    return parsed if parsed.tzinfo else parsed.astimezone()


def _speak_date(when):
    """Render a due date the way someone would say it."""
    today = datetime.now().astimezone().date()
    day = when.date()

    if day == today:
        prefix = "today"
    elif day == today + timedelta(days=1):
        prefix = "tomorrow"
    elif 0 < (day - today).days < 7:
        prefix = f"on {when.strftime('%A')}"
    else:
        prefix = f"on {when.strftime('%B')} the {_ordinal(day.day)}"
        # "January the 15th" for a date two years out is actively misleading.
        if day.year != today.year:
            prefix += f", {day.year}"

    if (when.hour, when.minute) == (0, 0):
        return prefix

    return f"{prefix} at {when.strftime('%H:%M')}"


def _ordinal(n):
    if 10 <= n % 100 <= 20:
        return f"{n}th"
    suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _count(n, noun):
    return f"{n} {noun}" + ("" if n == 1 else "s")


def _speak_names(cards):
    """Card names as a sentence, truncated so a long list stays listenable."""
    names = [card.name for card in cards[:SPEAK_LIMIT]]
    said = ". ".join(names)
    rest = len(cards) - len(names)
    return said + (f". And {rest} more" if rest > 0 else "")


if __name__ == "__main__":
    # Setup helper: run this to find the board id for .env, and to check that
    # the key and token actually work, without needing the rest of the Pi.
    import sys

    board = TrelloBoard()

    if not board._api_key:
        print("TRELLO_API_KEY is missing from .env")
        sys.exit(1)

    if not board._token:
        print("TRELLO_TOKEN is missing from .env. Open this URL, approve, and")
        print("paste the token it shows into .env as TRELLO_TOKEN:\n")
        print("  " + AUTHORIZE_URL.format(key=board._api_key))
        sys.exit(1)

    client = board._connect()
    print("Boards:")
    for b in client.list_boards():
        print(f"  {b.id}  {b.name}")

    if board._board_id or board._board_name:
        target = board._board()
        print(f"\nLists on {target.name}:")
        for lst in target.open_lists():
            print(f"  {lst.name} ({len(lst.list_cards())} cards)")

        print("\nOpen cards:")
        for card in target.open_cards():
            when = _card_due(card)
            print(f"  {card.name}" + (f"  [due {_speak_date(when)}]" if when else ""))
    else:
        print("\nSet TRELLO_BOARD_ID (or TRELLO_BOARD_NAME) in .env to go further.")
