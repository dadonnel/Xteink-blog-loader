# morning_sync service setup

## systemd (recommended)

Install and enable:

```bash
sudo cp deploy/systemd/morning-sync.service /etc/systemd/system/
sudo cp deploy/systemd/morning-sync.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now morning-sync.timer
```

Check status/logs:

```bash
systemctl status morning-sync.service
journalctl -u morning-sync.service -f
```

Optional environment overrides (set in service file under `[Service]`):

- `MORNING_SYNC_HOST`
- `MORNING_SYNC_UPLOAD_CMD`
- `MORNING_SYNC_GENERATOR_CMD`

## cron fallback

If systemd is unavailable, use `@reboot` so the script starts at boot and manages daily schedule internally:

```bash
@reboot cd /workspace/Xteink-blog-loader && /usr/bin/env python3 morning_sync.py >> /var/log/morning_sync.log 2>&1
```
