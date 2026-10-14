# Discord Voice Keeper

A Python bot that stays in one fixed Discord voice channel, muted and deafened.

## Behavior

- Joins automatically on every process start or hosting restart.
- `/join` enables automatic joining and recovery.
- `/leave` disconnects and pauses automatic joining until `/join` or a process restart.
- Only members with your configured role can use these commands; administrators do not bypass that check.
- Returns after an unexpected move or removal while automatic joining is enabled.
- Preserves existing sessions during gateway interruptions and allows the SDK to recover voice sessions.
- Uses progressively delayed retries and writes diagnostics to hosting logs.
- No audio playback, recording, database, persistent volume, or web server.

The built-in Discord timer is not controlled by this program. Timer preservation
and continuous operation within your free hosting allowance are not guaranteed.
The Discord integration and cloud deployment have not been run in the development
environment. Tests are omitted as requested.

## Discord setup

1. Create an application and bot at https://discord.com/developers/applications.
2. Invite it to your server using `bot` and `applications.commands` scopes.
3. Grant the bot **View Channel** and **Connect** in the target voice channel.
4. Leave privileged intents disabled; this bot uses only guild and voice-state events.
5. Enable Discord Developer Mode and copy your server, voice-channel, and control-role IDs.
6. Give your control role to anyone who should use `/join` and `/leave`.
7. Command users also need **Use Application Commands** in the text channel where they invoke commands.

Use a regular server voice channel, not a Stage channel or DM. Keep a slot
available if the voice channel has a user limit. Choosing `@everyone` as the
control role deliberately allows all server members to control the bot.

## Required variables

| Variable | Value |
| --- | --- |
| `DISCORD_TOKEN` | Raw bot token, without a `Bot ` prefix. |
| `DISCORD_GUILD_ID` | Your server ID. |
| `DISCORD_VOICE_CHANNEL_ID` | The one fixed voice channel ID. |
| `DISCORD_ALLOWED_ROLE_ID` | Role allowed to use both commands. |
| `LOG_LEVEL` | Optional; defaults to `INFO`. |

Keep the token in Railway variables or your local `.env`; never commit it.

## Railway deployment

1. Extract the project and put its contents at the root of a GitHub repository.
2. Create a separate Railway service from that repository.
3. Enter the four required variables before deploying.
4. Use the Dockerfile builder and the settings below.

| Setting | Value |
| --- | --- |
| Start command | `python main.py` |
| Replicas | 1, in one region |
| Restart policy | On Failure, maximum 10 retries for Free/Trial |
| Serverless/app sleeping | Disabled |
| Public domain / HTTP healthcheck | None |
| Volumes / databases | None |
| Deployment overlap | 0 seconds |
| Shutdown draining time | 20 seconds |

The Dockerfile uses Python 3.12 and installs `discord.py[voice]==2.7.1`.
Transitive dependencies resolve at install time; this is not a fully locked
package tree.

Railway's documentation marks its older Config as Code as deprecated, with
legacy support ending December 1, 2026. The included `railway.json` is for legacy
compatibility. For new services, enter the settings above in the dashboard.
If Railway rejects the legacy file, remove it; Docker and dashboard settings
are sufficient. Reference: https://docs.railway.com/config-as-code/reference

Teardown settings can also be supplied as service variables:
`RAILWAY_DEPLOYMENT_OVERLAP_SECONDS=0` and
`RAILWAY_DEPLOYMENT_DRAINING_SECONDS=20`.

Run only one copy with this token. Do not run locally at the same time as Railway.
For controlled updates, stop the old deployment before starting the new one;
rolling deployments may briefly overlap even with a single replica. Updates
and restarts can interrupt voice occupancy and the timer.

The worker has no paid API dependency, but cannot enforce Railway's billing.
Stay on a genuinely free plan and monitor the combined allowance used by this
bot and your webhook project. Credit exhaustion can stop the bot. Limited Trial
network restrictions may prevent voice connectivity. Bot commands cannot control
a stopped hosting process.

## Local run: Windows PowerShell

Use Python 3.12. From the project folder:

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Edit `.env` with your values, then start:

```powershell
.venv\Scripts\python.exe main.py --env-file .env
```

On Linux/macOS use `python3.12 -m venv .venv`, `cp .env.example .env`, and
`.venv/bin/python` in place of the Windows interpreter path. Stop with Ctrl+C.
Environment variables take precedence over local file entries.

## Recovery and logs

`voice_state=connected` indicates that the SDK and server voice state agree on
the target. `paused` indicates an intentional leave. `waiting_for_gateway` and
`recovering` indicate recovery in progress. Failures include a probable cause
and retry delay. Logging goes to stdout in UTC; the bot token is redacted.

During a stalled session, the SDK gets 90 seconds to recover before the controller
checks Discord's public status and gateway endpoints. A reported relevant incident
or failed reachability probe postpones forced replacement. After the grace period,
a still-unhealthy session may be replaced if there is no confirmed incident and
HTTPS is reachable. These checks cannot perfectly distinguish a Discord outage
from a hosting/network problem, and HTTPS reachability does not prove UDP health.

Application retry delays grow to a 60-second cap. The SDK also manages protocol
reconnections. `/leave` cancels pending application recovery and stops the voice
client; the process keeps running so it can receive `/join`.

## Code layout

| File | Responsibility |
| --- | --- |
| `main.py` | Configuration, logging, process startup and shutdown. |
| `config.py` | Environment settings and validation. |
| `bot.py` | Discord client, voice adapter, outage diagnostics. |
| `commands.py` | `/join`, `/leave`, and role authorization. |
| `voice_controller.py` | Connection lifecycle and cancellation. |
| `recovery.py` | Failure classification and retry timing. |
| `Dockerfile` | Container deployment. |
| `railway.json` | Optional legacy Railway configuration. |
