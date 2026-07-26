import time

import spotipy
from spotipy.oauth2 import SpotifyOAuth
from config import SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET, SPOTIFY_DEVICE_NAME

class SpotifyPlayer:
    def __init__(self):
        self._sp = spotipy.Spotify(auth_manager=SpotifyOAuth(
                client_id=SPOTIFY_CLIENT_ID,
                client_secret=SPOTIFY_CLIENT_SECRET,
                redirect_uri="http://127.0.0.1:8888/callback",
                scope="user-modify-playback-state user-read-playback-state",
                open_browser=False,
                cache_path=".spotify_token_cache"
            )
        )

        self._paused = True
        self._volume_target = self._get_volume()

    def toggle_pause_resume(self):
        """Toggle pause/resume."""
        if self._paused:
            self._sp.start_playback()
            self._paused = False
        else:
            self._sp.pause_playback()
            self._paused = True

    def play_song(self, song_name):
        """Search for a song and play it on the dev kit."""
        results = self._sp.search(q=song_name, type="track", limit=1)
        tracks = results["tracks"]["items"]
        if not tracks:
            print(f"No results for '{song_name}'")
            return

        track = tracks[0]
        device_id = self._get_device_id()
        if not device_id:
            print(f"Device '{SPOTIFY_DEVICE_NAME}' not found.")
            return

        self._sp.transfer_playback(device_id, force_play=False)
        self._sp.start_playback(device_id=device_id, uris=[track["uri"]])
        self._paused = False
        self._volume_target = self._get_volume()
        print(f"Playing: {track['name']} – {track['artists'][0]['name']}")

    def increase_volume(self):
        self._change_volume(5)

    def decrease_volume(self):
        self._change_volume(-5)

    def restart_song(self):
        self._sp.seek_track(0)

    def skip_song(self):
        self._sp.next_track()

    def cycle_playback_devices(self):
        """Move playback to the next available device, wrapping around."""
        devices = self._sp.devices()["devices"]
        if len(devices) < 2:
            print("Need at least two devices to cycle.")
            return

        # -1 when nothing is active, so the +1 below lands on the first device
        current = next((i for i, d in enumerate(devices) if d["is_active"]), -1)
        target = devices[(current + 1) % len(devices)]

        self._sp.transfer_playback(target["id"], force_play=not self._paused)
        time.sleep(0.5)  # Spotify needs a moment to register the handoff

        if target["volume_percent"] is not None:
            self._volume_target = target["volume_percent"]
        print(f"Playback moved to: {target['name']}")

    def _change_volume(self, delta):
        """"Change the volume of the device by delta."""
        playback = self._sp.current_playback()
        if not playback:
            return None
        current = playback["device"]["volume_percent"]
        if current is None:  # device doesn't report volume
            return None

        self._volume_target += delta
        self._sp.volume(self._volume_target)
        print("New volume:", self._volume_target)

    def _get_volume(self):
        """"Return the current volume of the device."""
        return self._sp.current_playback()["device"]["volume_percent"]

    def _get_device_id(self):
        """Find the dev kit's device ID by name."""
        for d in self._sp.devices()["devices"]:
            print('Device Name:', d)
            if d["name"] == SPOTIFY_DEVICE_NAME:
                return d["id"]

if __name__ == "__main__":
    player = SpotifyPlayer()
    player.play_song("Afterlife - Avenged Sevenfold")
