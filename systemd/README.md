# systemd (optional persistent service)

`./run.sh` is the simplest way to run everything. These `--user` units are for
when you want the observer to start on login and auto-restart on failure.

They assume the checkout is at `~/browser-agent-observer` (via `%h`). If yours
is elsewhere, edit the `WorkingDirectory`/`ExecStart` paths.

```bash
mkdir -p ~/.config/systemd/user
cp systemd/browser-agent-observer-*.service systemd/browser-agent-observer.target ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now browser-agent-observer.target
systemctl --user status 'browser-agent-observer-*'
```

Ports/hosts come from `~/browser-agent-observer/.env` (loaded via
`EnvironmentFile=-`, optional). The browser you observe is **not** managed here
— launch it yourself (`./run.sh browser`).

> `--user` services can be stopped at logout unless lingering is enabled:
> `sudo loginctl enable-linger $USER` (one-time) for them to survive a full
> logout/reboot.
