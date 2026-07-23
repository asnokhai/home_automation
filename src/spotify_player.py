import spotipy
from spotipy.oauth2 import SpotifyOAuth
from config import SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET, SPOTIFY_DEVICE_NAME

class SpotifyPlayer:
    def __init__(self):
        self._sp = spotipy.Spotify(auth_manager=SpotifyOAuth(
            client_id=SPOTIFY_CLIENT_ID,
            client_secret=SPOTIFY_CLIENT_SECRET,
            redirect_uri="http://127.0.0.1:8888/callback",
            scope="user-modify-playback-state user-read-playback-state"
        ))

    def pause(self):
        """Pause playback."""
        self._sp.pause_playback()
        print("Paused")

    def resume(self):
        """Resume playback."""
        self._sp.start_playback()
        print("Resumed")

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
        print(f"Playing: {track['name']} – {track['artists'][0]['name']}")

    def _get_device_id(self):
        """Find the dev kit's device ID by name."""
        for d in self._sp.devices()["devices"]:
            if d["name"] == SPOTIFY_DEVICE_NAME:
                return d["id"]

if __name__ == "__main__":
    player = SpotifyPlayer()
    player.play_song("Afterlife - Avenged Sevenfold")