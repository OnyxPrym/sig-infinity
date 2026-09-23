# Sig Infinity AI - Deployment Kit

## What's in this folder

    sig-infinity-deploy/
    └── backend/
        ├── Dockerfile              Container build
        ├── .dockerignore           Excludes .bak, logs, cloudflared.exe, etc.
        ├── docker-compose.yml      API + Cloudflare tunnel
        ├── .env                    YOUR EXACT VALUES (already filled in)
        ├── .env.example            Template (safe to commit)
        ├── requirements.txt        Python deps
        ├── entrypoint.sh           Refreshes token, then starts server
        ├── refresh_token.py        Headless Quotex login (Linux-ready)
        ├── server_patch.py         Copy these edits into your server.py
        └── backup.sh               Nightly SQLite backup script

## Next steps

1. Copy your existing files into `backend/`:
   - server.py
   - analysis.py
   - quotex_collector.py
   - data_source.py
   - mss.py
   - session.json (if you have a fresh one)
   - frontend/ (for Netlify)
   - templates/dashboard.html

2. Apply the patches from `server_patch.py` to your `server.py`
   (three small edits — see the file header).

3. Deploy on Oracle Cloud Free Tier:
       ssh ubuntu@<your-vm-ip>
       curl -fsSL https://get.docker.com | sudo sh
       sudo usermod -aG docker $USER && newgrp docker
       curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb -o cf.deb
       sudo dpkg -i cf.deb
       cloudflared tunnel login
       cloudflared tunnel create sig-infinity
       cloudflared tunnel route dns sig-infinity api.siginfinity.yourdomain.com
       cloudflared tunnel token sig-infinity  # paste into .env CLOUDFLARE_TUNNEL_TOKEN

4. Upload backend and start:
       scp -r backend ubuntu@<vm-ip>:~/sig-backend
       ssh ubuntu@<vm-ip>
       cd ~/sig-backend
       docker compose up -d --build
       docker compose logs -f sig-api

5. Add nightly backups:
       crontab -e
       0 3 * * * /home/ubuntu/sig-backend/backup.sh

6. Update frontend/index.html:
       const API_BASE = "https://api.siginfinity.yourdomain.com";
   Redeploy to Netlify.

## Rotate secrets later

When you're ready (per earlier warning), rotate these in .env:
- GITHUB_TOKEN
- ADMIN_PASSCODE
- FLASK_SECRET_KEY
- TWELVE_DATA_API_KEY
- QUOTEX_PASSWORD (and force-logout all Quotex sessions)