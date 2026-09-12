# Notifications

Merlin tells you when an agent needs you: a window flipping to **done** (the agent finished)
or **ask** (it is waiting for an answer). You get it on the desktop and on the phone, and
tapping the notification lands you in that window. Nothing is on until you turn it on.

## What you get

- **A count in the tab title and on the app icon.** `(2) worker-1 · term` means two windows
  want you. No permission needed, it is on everywhere.
- **Browser notifications while a tab is open.** One per transition, replaced (not stacked)
  when the same window flips again. Not shown for the window you are looking at.
- **Push to your devices with the tab closed.** The instance sends a Web Push to every
  device you subscribed. Clicking it opens Merlin on that window.

The signal is the same one the Sessions panel and the status-bar pills show: the tmux
`@agent_state` stamped by the agent hooks (see [Agent-state pills](terminal.md#agent-state-pills)).
Merlin sweeps it every two seconds, so a notification arrives within a couple of seconds.

## Turn it on

Open the terminal page and tap the **bell** in the bottom bar, left of the Sessions button.

1. **Notify in this browser**: the browser asks for permission once. This is per browser,
   and it is what shows notifications while a Merlin tab is open.
2. **Push to this device**: subscribes this browser to push. Works with every tab closed.
   Each device you subscribe is listed with a remove button, and **Send a test** sends one
   push right away so you can check the device gets it.

Both toggles are independent. A phone usually wants push only, a desktop both.

Push is quiet on purpose in two cases: no push for a window that a connected browser is
currently displaying, and no second push for the same window within 20 seconds (an agent
bouncing between busy and done at every prompt does not spam you).

## Install Merlin on a phone

Every Merlin instance installs as its own app, named after the machine (`Merlin · worker-1`),
so several instances sit side by side on a home screen.

**Android (Chrome).** Open Merlin, then the browser menu, then **Add to Home screen** (or
**Install app**). Push works from the browser too, installing is a comfort.

**iPhone and iPad (Safari, iOS 16.4 or later).** Open Merlin in Safari, tap **Share**, then
**Add to Home Screen**, then open Merlin from the home screen and enable push from the bell
there. iOS delivers Web Push only to installed web apps, which is why the bell in Safari
asks you to install first.

On a phone the installed app reconnects by itself when you come back to it after a
suspension: a short absence probes the connection, a long one replaces it.

## HTTPS is required

Service workers, push and browser notifications only work in a secure context: `https://`
or `http://localhost`. On Merlin Cloud every environment is served over HTTPS already. For a
self-hosted Merlin on a LAN address over plain HTTP you still get the title count and the
pills, and the bell says why the rest is off.

The simplest way to put TLS in front of a self-hosted Merlin is one Caddy block. Caddy gets
and renews the certificate on its own:

```
merlin.example.com {
    reverse_proxy localhost:3123
}
```

Point the DNS name at the machine and open port 443. Merlin itself keeps listening on
`localhost:3123`.

## Troubleshooting

- **The browser toggle is greyed out with "blocked".** The site's notification permission
  was denied at some point. Allow it in the browser's site settings (the lock icon in the
  address bar), then reload.
- **The bell says notifications need a running tmux server.** Merlin watches tmux windows,
  and none exist yet. Open the terminal once: it starts the server.
- **Plain HTTP on a LAN address.** Only the title count and the pills work. Put Merlin
  behind HTTPS (above) or use `http://localhost`.
- **Push arrives on the desktop but not on the phone.** On iPhone, open the app from the
  home screen and enable push from there, not from Safari. On Android, check the app's
  notification setting in the system settings.
- **A device in the list never gets a push.** Remove it and subscribe again from that
  device. A subscription the push service reports as gone is removed on its own.
- **Push is refused with `BadJwtToken` (self-hosted, iPhone or Mac).** Apple's push
  service checks that the sender names a real domain. Merlin uses the instance's public
  `https` URL when it knows it, and `https://merlincloud.dev` otherwise. Behind your own
  tunnel or reverse proxy, set `MERLIN_DASHBOARD_URL` to the public `https` URL of the
  instance (Settings, or `config.env`), then send a test again. Merlin Cloud environments
  need nothing.
- **Where the instance keeps it.** `~/.merlin/notifications/` holds the VAPID key pair and
  the subscriptions, both readable by your user only.
