#!/usr/bin/env python3

"""
soundcloud mp3 downloader - downloads tracks and playlists from soundcloud
uses requests, urllib, os, and optionally tqdm for progress bars

just run: python3 main.py <soundcloud_url> [-o OUTPUT_DIR]
examples:
  python3 main.py https://soundcloud.com/user/track-name
  python3 main.py https://soundcloud.com/user/sets/playlist -o my_music/
"""

import argparse
import os
import re
import sys
import time
from urllib.parse import urlparse, urljoin, urlencode

import requests

# tqdm makes nice progress bars but works without it too
try:
    from tqdm import tqdm
    has_tqdm = True
except ImportError:
    has_tqdm = False

# soundcloud stuff
soundcloud_home = "https://soundcloud.com"
soundcloud_api = "https://api-v2.soundcloud.com"
client_id_cache = os.path.join(os.path.expanduser("~"), ".soundcloud_client_id")


class SoundCloudError(Exception):
    """for user-friendly error messages"""
    pass


def debug(msg):
    # set to True if you need to see what's happening under the hood
    if False:
        print(f"[debug] {msg}", file=sys.stderr)


def clean_filename(name):
    """make sure filenames work on windows/mac/linux"""
    name = name.strip().replace("/", "-")
    name = re.sub(r'[<>:"\\|?*\x00-\x1F]', "", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name or "audio"


def get_cached_client_id():
    """try to read client id from cache file"""
    try:
        if os.path.isfile(client_id_cache):
            with open(client_id_cache, "r", encoding="utf-8") as f:
                cid = f.read().strip()
                if cid:
                    return cid
    except Exception:
        pass
    return None


def save_client_id(cid):
    """save client id for next time"""
    try:
        with open(client_id_cache, "w", encoding="utf-8") as f:
            f.write(cid.strip())
    except Exception:
        pass


def find_client_id(session):
    """scrape soundcloud website to find a working client id"""
    debug("looking for client id in soundcloud scripts...")
    r = session.get(soundcloud_home, timeout=15)
    r.raise_for_status()
    html = r.text

    # find all script tags in the page
    scripts = re.findall(r'<script[^>]+src="([^"]+)"', html)
    script_urls = []
    for s in scripts:
        if s.startswith("//"):
            script_urls.append("https:" + s)
        elif s.startswith("http"):
            script_urls.append(s)
        else:
            script_urls.append(urljoin(soundcloud_home, s))

    # also check some other pages that might have different scripts
    extra_pages = ["/discover", "/charts/top"]
    for page in extra_pages:
        try:
            rr = session.get(urljoin(soundcloud_home, page), timeout=15)
            if rr.ok:
                more_scripts = re.findall(r'<script[^>]+src="([^"]+)"', rr.text)
                for s in more_scripts:
                    if s.startswith("//"):
                        script_urls.append("https:" + s)
                    elif s.startswith("http"):
                        script_urls.append(s)
                    else:
                        script_urls.append(urljoin(soundcloud_home, s))
        except Exception:
            pass

    # remove duplicates
    seen = set()
    script_urls = [u for u in script_urls if not (u in seen or seen.add(u))]

    # patterns that might find a client id
    patterns = [
        re.compile(r'client_id\s*:\s*"([0-9a-zA-Z]{32})"'),
        re.compile(r'client_id\s*=\s*"([0-9a-zA-Z]{32})"'),
        re.compile(r'"client_id"\s*:\s*"([0-9a-zA-Z]{32})"'),
        re.compile(r'client_id=([0-9a-zA-Z]{32})')
    ]

    # check the html first
    for pattern in patterns:
        match = pattern.search(html)
        if match:
            return match.group(1)

    # then check the javascript files
    for i, js_url in enumerate(script_urls[:20]):
        try:
            debug(f"checking script {i+1}/{min(20, len(script_urls))}: {js_url}")
            js_response = session.get(js_url, timeout=15)
            if not js_response.ok or not js_response.text:
                continue
            js_code = js_response.text
            for pattern in patterns:
                match = pattern.search(js_code)
                if match:
                    return match.group(1)
        except Exception:
            continue

    raise SoundCloudError("couldn't find a soundcloud client id. try again later.")


def get_client_id(session):
    """get a working client id, from cache or by scraping"""
    def check_client_id_valid(cid):
        """test if a client id actually works"""
        try:
            url = f"{soundcloud_api}/search/tracks?{urlencode({'q': 'test', 'limit': 1, 'client_id': cid})}"
            response = session.get(url, timeout=15)
            if response.status_code in (401, 403):
                return False
            return response.ok
        except Exception:
            return False

    # try cached id first
    cached_id = get_cached_client_id()
    if cached_id and check_client_id_valid(cached_id):
        debug("using cached client id")
        return cached_id

    debug("scraping for new client id...")
    new_id = find_client_id(session)
    if not check_client_id_valid(new_id):
        # sometimes the first one doesn't work, try one more time
        time.sleep(0.5)
        new_id = find_client_id(session)
        if not check_client_id_valid(new_id):
            raise SoundCloudError("found client id but it doesn't work. try again in a minute.")

    save_client_id(new_id)
    return new_id


def api_request(session, url, params=None, allow_404=False):
    """make a request to soundcloud api"""
    try:
        response = session.get(url, params=params, timeout=20)
        if response.status_code == 404 and allow_404:
            return None
        response.raise_for_status()
        return response.json()
    except requests.exceptions.HTTPError:
        if response.status_code in (401, 403):
            raise SoundCloudError("access denied - client id might be expired. try again.")
        raise
    except requests.exceptions.RequestException as e:
        raise SoundCloudError(f"network error: {e}")


def resolve_soundcloud_url(session, client_id, url):
    """convert a soundcloud url to api data"""
    endpoint = f"{soundcloud_api}/resolve"
    params = {"url": url, "client_id": client_id}
    data = api_request(session, endpoint, params=params)
    if not data:
        raise SoundCloudError("couldn't resolve that soundcloud url.")
    return data


def find_mp3_transcoding(track_data):
    """look for a downloadable mp3 in the track data"""
    media = track_data.get("media") or {}
    transcodings = media.get("transcodings") or []
    for transcode in transcodings:
        fmt = transcode.get("format") or {}
        mime_type = fmt.get("mime_type", "")
        protocol = fmt.get("protocol", "")
        if "audio/mpeg" in mime_type and protocol == "progressive":
            return transcode
    return None


def get_track_download_info(session, client_id, track_data):
    """get the actual mp3 url and track info"""
    title = track_data.get("title", "audio")
    user = track_data.get("user") or {}
    artist = user.get("username", "").strip() or "Unknown Artist"

    transcoding = find_mp3_transcoding(track_data)
    if not transcoding:
        return None  # no mp3 available for this track

    # get the final mp3 url
    transcode_url = transcoding.get("url")
    separator = "&" if "?" in transcode_url else "?"
    final_transcode_url = f"{transcode_url}{separator}client_id={client_id}"

    transcode_data = api_request(session, final_transcode_url)
    mp3_url = transcode_data.get("url")
    if not mp3_url:
        raise SoundCloudError("soundcloud didn't give us an mp3 url.")

    # create a nice filename
    filename = clean_filename(f"{artist} - {title}") + ".mp3"
    return {"url": mp3_url, "title": title, "artist": artist, "filename": filename}


def download_file(session, url, save_path):
    """download a file with progress tracking"""
    with session.get(url, stream=True, timeout=60) as response:
        response.raise_for_status()
        total_size = int(response.headers.get("Content-Length", "0")) or None
        chunk_size = 1024 * 64

        if has_tqdm and total_size:
            # fancy progress bar with tqdm
            with tqdm(total=total_size, unit="B", unit_scale=True, desc=os.path.basename(save_path)) as progress_bar:
                with open(save_path, "wb") as f:
                    for chunk in response.iter_content(chunk_size=chunk_size):
                        if chunk:
                            f.write(chunk)
                            progress_bar.update(len(chunk))
        else:
            # basic progress display
            downloaded = 0
            last_update = time.time()
            with open(save_path, "wb") as f:
                for chunk in response.iter_content(chunk_size=chunk_size):
                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)
                        now = time.time()
                        if now - last_update > 0.5:
                            if total_size:
                                percent = (downloaded / total_size) * 100
                                print(f"  {downloaded}/{total_size} bytes ({percent:.1f}%)", end="\r")
                            else:
                                print(f"  {downloaded} bytes", end="\r")
                            last_update = now
            print()


def make_sure_dir_exists(path):
    """create directory if it doesn't exist"""
    if path:
        os.makedirs(path, exist_ok=True)


def is_valid_soundcloud_url(url):
    """check if this looks like a real soundcloud url"""
    try:
        parsed = urlparse(url)
        return parsed.scheme in ("http", "https") and "soundcloud.com" in parsed.netloc.lower()
    except Exception:
        return False


def download_track(session, client_id, track_data, output_dir):
    """download a single track"""
    track_info = get_track_download_info(session, client_id, track_data)
    if not track_info:
        title = track_data.get("title", "Unknown")
        artist = (track_data.get("user") or {}).get("username", "Unknown")
        print(f"skipping (no mp3 available): {artist} - {title}")
        return False

    save_path = os.path.join(output_dir, track_info["filename"])
    
    # avoid overwriting files
    base, ext = os.path.splitext(save_path)
    counter = 2
    while os.path.exists(save_path):
        save_path = f"{base} ({counter}){ext}"
        counter += 1

    print(f"downloading: {track_info['artist']} - {track_info['title']}")
    download_file(session, track_info["url"], save_path)
    print(f"saved: {save_path}")
    return True


def handle_download(session, client_id, resolved_data, output_dir):
    """handle either a single track or a playlist"""
    item_type = resolved_data.get("kind")
    success_count = 0
    total_count = 0

    if item_type == "track":
        total_count = 1
        if download_track(session, client_id, resolved_data, output_dir):
            success_count += 1

    elif item_type in ("playlist", "system-playlist"):
        tracks = resolved_data.get("tracks") or []
        total_count = len(tracks)
        if not total_count:
            raise SoundCloudError("this playlist has no tracks.")
        for i, track in enumerate(tracks, start=1):
            print(f"[{i}/{total_count}]")
            if download_track(session, client_id, track, output_dir):
                success_count += 1

    else:
        raise SoundCloudError(f"unsupported soundcloud type: {item_type}")

    print(f"done. downloaded {success_count}/{total_count} tracks.")
    if success_count < total_count:
        print("some tracks were skipped (probably no mp3 available).")


def fix_shortlink(url):
    """convert soundcloud shortlinks to regular urls"""
    if "on.soundcloud.com" not in url:
        return url
    try:
        with requests.Session() as s:
            s.max_redirects = 5
            # try head first, then get if needed
            response = s.head(url, allow_redirects=True, timeout=10)
            if (not response.ok or "soundcloud.com" not in response.url) and response.is_redirect:
                response = s.get(url, allow_redirects=True, timeout=10)
            if response.ok and response.url and "soundcloud.com" in response.url:
                print(f"fixed shortlink → {response.url}")
                return response.url
            else:
                print("warning: couldn't fix shortlink, using original url")
                return url
    except Exception as e:
        print(f"warning: shortlink fix failed ({e}), using original url")
        return url


def main():
    parser = argparse.ArgumentParser(
        description="download mp3s from soundcloud",
        add_help=True
    )
    parser.add_argument("url", nargs="?", help="soundcloud track or playlist url")
    parser.add_argument("-o", "--output", default=".", help="where to save files (default: current folder)")

    # ignore extra args that might come from IDEs
    args, _ = parser.parse_known_args()

    if not args.url:
        # ask for url if not provided
        try:
            args.url = input("enter soundcloud url: ").strip()
        except EOFError:
            print("error: need a soundcloud url")
            sys.exit(2)

    url = args.url.strip()
    url = fix_shortlink(url)

    if not is_valid_soundcloud_url(url):
        print("error: please provide a valid soundcloud url")
        sys.exit(1)

    make_sure_dir_exists(args.output)

    # set up http session with realistic browser headers
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/124.0 Safari/537.36"
    })

    try:
        client_id = get_client_id(session)
        resolved_data = resolve_soundcloud_url(session, client_id, url)
        handle_download(session, client_id, resolved_data, args.output)

    except SoundCloudError as e:
        print(f"error: {e}")
        sys.exit(1)
    except requests.exceptions.RequestException as e:
        print(f"network error: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        print("\ncancelled by user")
        sys.exit(130)
    except Exception as e:
        print(f"unexpected error: {e.__class__.__name__}: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()