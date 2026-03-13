# ytcast-py

Small command-line tool for sending a YouTube video from Linux to a Hisense TV.

It is aimed at the common case where the TV is already on, the YouTube app is available on the TV, and you want to send a video quickly from the terminal.

## Files

- `ytcast.py`: main Python script
- `ytcast`: simple shell wrapper for daily use
- `fix_hisense_403.py`: compatibility entry point kept for older commands

## Usage

Run the wrapper:

```bash
./ytcast dQw4w9WgXcQ
```

or pass a full YouTube URL:

```bash
./ytcast 'https://youtu.be/dQw4w9WgXcQ'
./ytcast 'https://www.youtube.com/watch?v=dQw4w9WgXcQ'
```

You can also run the Python script directly:

```bash
python3.13 ytcast.py dQw4w9WgXcQ
```

## Behavior

- With no arguments, the script prints help and asks whether it should try the default TV IP and send a test video.
- With one plain argument, it treats that argument as the video to send.
- It accepts common YouTube formats such as a plain video id, `youtu.be/...`, `youtube.com/watch?...`, and `shorts/...`.
- It tries multiple known TV endpoints and only gives up after all known paths fail.

## Notes

- The current default TV IP is `192.168.10.191`.
- On this TV, port `7000` is AirPlay, not the working YouTube DIAL endpoint.
- The working path currently uses the TV's YouTube app plus YouTube Lounge control.

