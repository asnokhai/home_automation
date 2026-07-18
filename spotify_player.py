import spotipy
from spotipy.oauth2 import SpotifyOAuth
from config import SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET

class SpotifyPlayer:
    def __init__(self):
        self._sp = spotipy.Spotify(auth_manager=SpotifyOAuth(
            client_id=SPOTIFY_CLIENT_ID,
            client_secret=SPOTIFY_CLIENT_SECRET,
            redirect_uri="http://127.0.0.1:8888/callback",
            scope="user-modify-playback-state user-read-playback-state"
        ))

    def play(self, song_name):
        """Search for a song by name and play it on the active device."""
        results = self._sp.search(q=song_name, type="track", limit=1)
        tracks = results["tracks"]["items"]
        if not tracks:
            print(f"No results for '{song_name}'")
            return

        track = tracks[0]
        devices = self._sp.devices()["devices"]
        if not devices:
            print("No Spotify devices found. Open Spotify somewhere first.")
            return

        device_id = devices[0]["id"]
        self._sp.start_playback(device_id=device_id, uris=[track["uri"]])
        print(f"Playing: {track['name']} – {track['artists'][0]['name']} on {devices[0]['name']}")

if __name__ == "__main__":
    player = SpotifyPlayer()
    player.play("Afterlife - Avenged Sevenfold")