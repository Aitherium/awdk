---
name: awdesk
description: >-
  Put the agent on someone's desk: install the Aither desktop companion from its
  release assets, keep it talking to a daemon, and use decision cards as the
  place a question actually reaches a person instead of scrolling past in a
  transcript. Use when an agent needs a human answer at the right moment, when a
  tray/avatar surface should show what the agent is doing, or when someone asks to
  "put it on my desktop".
---
<!-- Generated from the awskills pack (skills/awdesk.md, github.com/Aitherium/awskills). Edit the pack, never this copy. -->

# awdesk — the agent on your desk

awdesk is a tray app: an icon, an avatar, and a **decision card** that pops when
the agent needs a person. That last part is the reason it exists. A question that
lands as a card gets answered; a question buried in a long transcript does not.

It is a client. It shows and asks; the work happens behind the daemon it talks to.

Source: <https://github.com/Aitherium/awdesk>

## Install

Download the asset for the platform from the
[releases page](https://github.com/Aitherium/awdesk/releases), and check the
download against `SHA256SUMS.txt` in the same release.

From source:

```bash
git clone https://github.com/Aitherium/awdesk
cd awdesk
npm install && npx electron .
```

## Use it for what it is good at

- **Approval cards.** Anything that changes state and needs a yes goes here, not
  into a chat window.
- **Presence.** A tray icon tells a person something is running, without them
  reading a log.
- **Hand-off.** When the agent is stuck on a judgement call, the card is the
  hand-off point.

## Traps

- **An empty desk is usually a missing daemon.** Check what it should be talking
  to before reinstalling.
- **One install at a time.** A source build running alongside an installed copy
  gives two tray icons and two bridges, and it is rarely obvious which one
  answered.
- **Signing varies by channel.** Verify the hash rather than clicking through an
  installer warning.
- **Do not put a decision card behind another window** and call the agent
  unresponsive. The card has to be able to reach the front.

## What it does not do

It serves no tools. If you need a capability, it lives in the daemon — awdesk is
how a person sees and answers.
