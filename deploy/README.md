# Deployment Configurations

This directory contains deployment-specific configurations.

## Structure

```
deploy/
├── vps/                    # Hetzner VPS deployment
│   ├── docker-compose.yml  # All Control Plane services
│   └── Caddyfile           # HTTPS reverse proxy config
├── edge-4070/              # RTX 4070 PC deployment
│   ├── docker-compose.yml  # Frigate + Edge Agent
│   └── frigate/
│       └── config.yml      # Frigate config template
└── tailscale/
    └── setup.md            # Tailscale setup guide
```

## Deployment Order

1. **VPS first**: Provision Hetzner VPS, deploy Control Plane
2. **Edge PC**: Setup RTX 4070 PC with Ubuntu + Docker
3. **Tailscale**: Connect both via encrypted tunnel
4. **Verify**: Test communication between VPS and PC

See [../docs/DEPLOYMENT.md](../docs/DEPLOYMENT.md) for full guide.