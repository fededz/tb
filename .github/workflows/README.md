# CI/CD Workflows

## Overview

Two GitHub Actions workflows handle testing and deployment:

- **test.yml** -- Runs `pytest` on every pull request targeting `main`. No deploy.
- **deploy.yml** -- Runs `pytest` then deploys to EC2 on every push to `main`.

## Deploy Flow

```
Push to main
  -> test job: install deps, run pytest
  -> deploy job (if tests pass):
       1. SSH into EC2
       2. git pull origin main
       3. docker compose up -d --build
       4. Verify containers are running
       5. Wait 10s, check logs for errors
       6. curl the dashboard health endpoint
```

## Required GitHub Secrets

Configure these in **Settings > Secrets and variables > Actions**:

| Secret | Description |
|--------|-------------|
| `EC2_SSH_KEY` | Private SSH key (PEM format) for the `ec2-user` account on the EC2 instance |
| `EC2_HOST` | Public IP address or hostname of the EC2 instance |
| `DASHBOARD_USER` | Username for dashboard basic auth |
| `DASHBOARD_PASSWORD` | Password for dashboard basic auth |

## EC2 Instance Setup (First Time)

1. **SSH access**: Generate a key pair. Add the public key to `~/.ssh/authorized_keys` on the EC2 instance for `ec2-user`. Store the private key as the `EC2_SSH_KEY` secret in GitHub.

2. **Install dependencies on EC2**:
   ```bash
   sudo yum update -y
   sudo yum install -y docker git
   sudo systemctl enable docker && sudo systemctl start docker
   sudo usermod -aG docker ec2-user

   # Install docker compose plugin
   sudo mkdir -p /usr/local/lib/docker/cli-plugins
   sudo curl -SL https://github.com/docker/compose/releases/latest/download/docker-compose-linux-x86_64 \
     -o /usr/local/lib/docker/cli-plugins/docker-compose
   sudo chmod +x /usr/local/lib/docker/cli-plugins/docker-compose
   ```

3. **Clone the repo**:
   ```bash
   sudo mkdir -p /opt/trading-bot
   sudo chown ec2-user:ec2-user /opt/trading-bot
   cd /opt/trading-bot
   git clone <repo-url> .
   ```

4. **Create the `.env` file** on the EC2 instance at `/opt/trading-bot/.env` with all required environment variables (PPI keys, DB credentials, Telegram token, etc.). This file is never committed to the repo.

5. **Initial start**:
   ```bash
   cd /opt/trading-bot
   docker compose up -d --build
   ```

6. **Security group**: Ensure the EC2 security group allows:
   - Inbound SSH (port 22) from GitHub Actions IP ranges (or use a bastion/VPN)
   - Inbound TCP port 9091 for the dashboard (restrict to your IP)
   - Outbound: all (for PPI API, Telegram, etc.)

## Troubleshooting

- **Deploy fails at SSH step**: Check that `EC2_SSH_KEY` is correct (include the full PEM with header/footer lines) and that the EC2 security group allows SSH from GitHub Actions runners.
- **Health check fails**: SSH into the instance and check `docker compose logs trading` for startup errors. Verify the `.env` file is present and correct.
- **Tests fail**: Run `pytest tests/ -v` locally to reproduce.
