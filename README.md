This python code for the VTR - Virtual Tape system is a video playback system like a Cart system in low to no budget community tv station aka "Public Access"
Mov files are loaded over the smb to the vtr. The code loads cmd for Blackmagic DeckLink with SDI / HDMI out. 
Clips loaded are short stories for news cast, program opens and other video playback uses.
This system is a playback only system and is great for video production switchers that do not have a built-in DDR Playback.
The VTR holds the python program - a remote computer / touch screen IPs to the VTR via SSH.

Mock display below

```
┌──────────┬──────────┬──────────┬────────────┐
│  FILES   │  QUEUE   │ PREVIEW  │ ▲ UP       │
│          │          │          ├────────────┤
│          │          │          │ ▼ DOWN     │
│          │          │          ├────────────┤
│          │          │          │ ENTER      │
│          │          │          ├────────────┤
│          │          │          │ BREAK      │
├──────────┴──────────┴──────────┤ STOP       │
│       ▶ VTR PLAYING            ├────────────┤
│       REMAIN  01:42            │ CLEAR      │
│ ███████████████░░░░░░░         ├────────────┤
│ NOW: video.mov                 │ SLATE      │
│ NEXT: nextvideo.mov            ├────────────┤
│ CLOCK / STATUS                 │            │
│                                │    PLAY    │
│                                │            │
└────────────────────────────────┴────────────┘
```
