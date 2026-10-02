---
name: merlin-app
description: Run, test or show a Linux GUI app or game (e.g. a game you are building) on a private display with `merlin app` - launch it, take screenshots, press keys, click, read its logs, and let the user watch and play it live in the Merlin terminal or on their phone.
user-invocable: false
allowed-tools: Bash, Read
---

# Apps: run and drive GUI apps

`merlin app` runs a graphical app on its own invisible display (never on the
user's desktop). You drive it with screenshots and input; the user watches the
same app live in the Merlin dashboard, next to your terminal window, and can
play it from their phone. Every command prints JSON.

## The loop

```bash
merlin app run --name oob -- ./build/OutOfBody     # from the app's folder (or --cwd DIR)
merlin app screenshot oob                          # -> {"path": "...png"}
# Read the PNG to see the screen, then act:
merlin app input oob key Right                     # X keysym names
merlin app input oob key Right --repeat 3 --delay 150
merlin app input oob click 640 360                 # display pixels, same as the screenshot
merlin app screenshot oob                          # check the effect
```

- `run` returns as soon as the app's window is up; the app keeps running after
  the command ends. Status `exited` (exit 1) means it crashed at start: the
  JSON carries `log_tail`.
- After a rebuild, relaunch with `merlin app run --replace --name oob -- ...`.
- `merlin app logs oob [--tail 50] [--follow]` shows stdout and stderr, also
  after a crash. `merlin app list` shows every app with its status and exit code.
- `merlin app stop oob` when you are done. Do not leave apps running needlessly:
  each one holds a display and, for games, the GPU.

## Input

- `key KEY...`: press and release in order. Names: `Right Left Up Down Return
  Escape Tab space BackSpace`, letters (`x`, `z`), combos (`ctrl+z`,
  `shift+Tab`). `--repeat N`, `--delay MS`.
- `type "text"`, `click X Y [--button 3]`, `move X Y`.
- Games often miss keys sent faster than a frame or two. When a key seems
  lost, hold it (`--hold 80`) and space presses out (`--delay 150`), then take
  a screenshot to confirm.
- The app runs as the user, with the user's files: a game loads and writes the
  user's real saves and settings. Prefer a new game or a throwaway save, and
  say so when you had to touch an existing one.

## Help the user play it

The user's player shows touch controls. Set them at launch so they fit the app:

```bash
merlin app run --name oob --controls gamepad --keys A=x,B=z,X=r,Y=Tab,Start=Escape -- ./build/OutOfBody
```

- `--controls gamepad` (D-pad = arrow keys, buttons mapped by `--keys`),
  `trackpad` (laptop-trackpad pointer, for desktop apps) or `touch` (tap where
  you touch).
- `--size 1920x1080` changes the display size (default 1280x720).
- `--gpu off` forces software rendering (default `auto` uses the GPU).
- The app's sound goes to the user's player (default `--audio stream`; the
  app is then silent on the machine). `--audio local` plays it on the machine
  instead. A note on stderr says when sound cannot be streamed (no PipeWire).

The command's JSON has a `url` (`/apps/<id>/play`): the full-screen player. Give
the user that path on top of `merlin dashboard-url` when they ask where to
watch; otherwise they see it in the terminal's ▶ button automatically.
