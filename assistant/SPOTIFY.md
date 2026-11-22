# Spotify for Azel

Azel plays and controls Spotify by voice ("아이유 밤편지 틀어줘", "다음 곡", "볼륨 30") through the Spotify Web API,
on the gateway's own OAuth grant (`gateway/oauth.py`, `gateway/spotify.py`). It works on every model path (Qwen,
Claude API, NIM, `claude -p`).

## What you need

- **Spotify Premium.** Playback control (play, pause, skip, volume) is Premium-only. Under Spotify's 2026
  Development Mode rules the app owner also needs Premium, or the app stops working.
- **An awake Spotify app** on some device: the Mac or iPhone app, or a Spotify Connect speaker. With nothing open,
  Spotify answers "no active device" and Azel asks you to open Spotify once.

## One-time setup

1. Open <https://developer.spotify.com/dashboard> and log in with your Spotify account.
2. **Create app**:
   - App name: `Azel`; description: `Personal voice assistant`.
   - **Redirect URIs**: add the ones you will use (Spotify must match them exactly):
     - `https://azel.scian.xyz/v1/oauth/callback`: works from any device, through the public proxy. Needs
       `BISEO_PUBLIC_URL=https://azel.scian.xyz` on the Mac gateway and the proxy passing
       `GET /v1/oauth/callback` (DevOps).
     - `azel://oauth/callback`: the app's own scheme, once the app forwards it. Add it if the dashboard accepts it.
     - `http://127.0.0.1:43110/callback` (optional): only for a loopback listener on the Mac. Spotify accepts
       `127.0.0.1`, not `localhost`.
   - **Which API/SDKs are you planning to use?**: tick **Web API**.
   - Accept the terms and **Save**.
3. In the app's **Settings**, copy the **Client ID**. There is no client secret to copy: Azel uses PKCE.
4. **User Management**: add your own Spotify account email. Development Mode allows a handful of listed users.
5. In the Azel app, open the connectors screen, tap **Spotify → Connect**, and paste the Client ID the first time.
   The app calls `POST /v1/connectors/spotify/authorize` with `{"client_id": "…", "redirect_uri": "…"}`, opens the
   returned URL, you tap **Agree** on Spotify's page, and the connector shows **connected**.

Until the app has its Connect button, you can do it from a terminal on the tailnet:

```bash
TOKEN=$(cat ~/.config/biseo/token)
curl -s -X POST http://100.64.0.8:8892/v1/connectors/spotify/authorize \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"client_id":"<your client id>","redirect_uri":"https://azel.scian.xyz/v1/oauth/callback"}'
```

Open the printed `url` in a browser and agree. The callback page says 연결을 마쳤어요.

## Where things live

- `~/.config/biseo/oauth/spotify.json` (0600; folder 0700): client id, access and refresh tokens, expiry. It stays on
  this gateway: it is never returned over the API, logged, or sent anywhere but Spotify. The gateway refreshes the
  token a few minutes before it expires.
- Status: `GET /v1/connectors` → `"oauth": {"spotify": "needs_consent" | "connected" | "expired"}`. `expired` means
  Spotify refused the refresh (access revoked, or the owner lost Premium): connect again.
- Disconnect: `DELETE /v1/connectors/spotify/oauth`. Also remove Azel at
  <https://www.spotify.com/account/apps/>.

## Voice tools

| tool | input | does |
|---|---|---|
| `spotify_play` | `query` (the user's words) or `uri` (`spotify:track|album|artist|playlist:…`), `device?` | an exact artist name plays the artist; "…플레이리스트" searches playlists; otherwise the top track |
| `spotify_control` | `action`: pause, resume, next, previous, volume (`volume` 0–100), shuffle (`on`) | |
| `spotify_now_playing` | — | song, artist, device, playing or paused |
| `spotify_devices` | — | the devices Spotify can play on now |

With no device active, `spotify_play` starts on the first available one. `device` matches part of a device name
("맥북", "iPhone").
