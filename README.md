# digital hoarder

A straightforward Python script to download SoundCloud tracks and playlists  
(not sure about playlists because I haven't set up dependencies for that yet,  
but you can figure that out as well) as MP3 files - locally.

## what it does
- takes a SoundCloud URL (track or playlist)
- automatically finds a valid `client_id`
- downloads the audio in MP3 format (if available)
- saves files locally with sensible filenames
- works offline after dependencies are installed

## requirements
```bash
python3 -m pip install requests tqdm
INTERNET CONNECTION !!
