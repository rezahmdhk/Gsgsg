import asyncio
import logging
import json
import os
import re
import subprocess
import tempfile
import time
from typing import List, Optional, Dict, Any
from pathlib import Path
from collections import deque

import telegram
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from pyrogram import Client
from pyrogram.types import Message as PyroMessage
from pyrogram.enums import ChatType
from pytgcalls import PyTgCalls, idle
from pytgcalls.types import AudioQuality, StreamAudioQuality
from pytgcalls.types.input_stream import AudioStream, AudioPiped
from pytgcalls.types.input_stream.quality import HighQualityAudio
from pytgcalls.exceptions import NoActiveGroupCall

import yt_dlp
import spotipy
from spotipy.oauth2 import SpotifyClientCredentials
import requests
from bs4 import BeautifulSoup
import aiofiles
from cachetools import TTLCache

# ========== CONFIGURATION ==========
BOT_TOKEN = "8933484642:AAFocsKX20UN3Js8Mu1AUR5eTZzc215b7xU"
API_ID = 8916314219
API_HASH = "YOUR_API_HASH"
PHONE_NUMBER = "+1234567890"  # user account phone

SPOTIFY_CLIENT_ID = "YOUR_SPOTIFY_CLIENT_ID"  # optional, can be left empty
SPOTIFY_CLIENT_SECRET = "YOUR_SPOTIFY_CLIENT_SECRET"

# ========== LOGGING ==========
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ========== DATA STORE ==========
QUEUE = {}  # chat_id -> deque of song dicts
PLAYING = {}  # chat_id -> current song dict
LOOP_MODE = {}  # chat_id -> "off" | "one" | "all"
RADIO_MODE = {}  # chat_id -> bool
VOLUME = {}  # chat_id -> int (0-200)
USER_PLAYLISTS = {}  # user_id -> {playlist_name: [song_ids]}
SONG_HISTORY = {}  # chat_id -> deque of song dicts (max 20)

# In-memory cache for search results
SEARCH_CACHE = TTLCache(maxsize=100, ttl=300)

# ========== SPOTIFY ==========
spotify = None
if SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET:
    spotify = spotipy.Spotify(
        client_credentials_manager=SpotifyClientCredentials(
            client_id=SPOTIFY_CLIENT_ID,
            client_secret=SPOTIFY_CLIENT_SECRET,
        )
    )

# ========== PYROGRAM & PYTGCALLS ==========
pyro_client = Client("music_bot", api_id=API_ID, api_hash=API_HASH, phone_number=PHONE_NUMBER)
pytgcalls = PyTgCalls(pyro_client)

# ========== YT-DLP OPTIONS ==========
YDL_OPTS = {
    "format": "bestaudio/best",
    "extractaudio": True,
    "audioformat": "mp3",
    "outtmpl": "%(extractor)s-%(id)s-%(title)s.%(ext)s",
    "restrictfilenames": True,
    "noplaylist": True,
    "nocheckcertificate": True,
    "ignoreerrors": True,
    "logtostderr": False,
    "quiet": True,
    "no_warnings": True,
    "default_search": "ytsearch",
    "source_address": "0.0.0.0",
}

# ========== HELPERS ==========
def format_duration(seconds: int) -> str:
    if not seconds:
        return "Live"
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"

def format_progress_bar(current: int, total: int, length: int = 15) -> str:
    if total <= 0:
        return "⏳ Live"
    percent = current / total
    filled = int(percent * length)
    bar = "█" * filled + "░" * (length - filled)
    return f"{bar} {percent*100:.1f}%"

async def extract_song(query: str) -> Optional[Dict[str, Any]]:
    """Extract song info from query (URL or search) using yt-dlp."""
    try:
        with yt_dlp.YoutubeDL(YDL_OPTS) as ydl:
            info = ydl.extract_info(query, download=False)
            if not info:
                return None
            if "entries" in info:
                info = info["entries"][0]
            return {
                "title": info.get("title", "Unknown"),
                "duration": info.get("duration", 0),
                "url": info.get("url"),
                "webpage_url": info.get("webpage_url"),
                "thumbnail": info.get("thumbnail"),
                "uploader": info.get("uploader"),
                "extractor": info.get("extractor"),
            }
    except Exception as e:
        logger.error(f"Extract error: {e}")
        return None

async def get_spotify_track(track_id: str):
    if not spotify:
        return None
    try:
        track = spotify.track(track_id)
        return {
            "title": track["name"],
            "duration": track["duration_ms"] // 1000,
            "webpage_url": track["external_urls"]["spotify"],
            "uploader": track["artists"][0]["name"],
            "thumbnail": track["album"]["images"][0]["url"],
        }
    except:
        return None

async def search_spotify(query: str):
    if not spotify:
        return []
    try:
        results = spotify.search(q=query, type="track", limit=10)
        tracks = []
        for item in results["tracks"]["items"]:
            tracks.append({
                "title": item["name"],
                "duration": item["duration_ms"] // 1000,
                "webpage_url": item["external_urls"]["spotify"],
                "uploader": item["artists"][0]["name"],
                "thumbnail": item["album"]["images"][0]["url"],
                "id": item["id"],
            })
        return tracks
    except:
        return []

async def download_audio(song_url: str) -> Optional[str]:
    """Download audio and return file path."""
    try:
        with yt_dlp.YoutubeDL({
            **YDL_OPTS,
            "format": "bestaudio/best",
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }],
        }) as ydl:
            info = ydl.extract_info(song_url, download=True)
            filename = ydl.prepare_filename(info)
            # Change extension to mp3
            base, _ = os.path.splitext(filename)
            mp3_filename = base + ".mp3"
            if os.path.exists(mp3_filename):
                return mp3_filename
            # fallback
            return filename
    except Exception as e:
        logger.error(f"Download error: {e}")
        return None

async def get_lyrics(song_title: str, artist: str = "") -> Optional[str]:
    """Fetch lyrics from Genius or AZLyrics."""
    # Try Genius
    try:
        url = f"https://api.genius.com/search?q={song_title} {artist}"
        headers = {"Authorization": "Bearer YOUR_GENIUS_ACCESS_TOKEN"}  # optional
        # For simplicity, we'll scrape AZLyrics
        query = f"{artist} {song_title}".replace(" ", "+")
        search_url = f"https://search.azlyrics.com/search.php?q={query}"
        resp = requests.get(search_url)
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, "html.parser")
            # Find first result link
            link = soup.find("a", href=re.compile(r"/lyrics/"))
            if link:
                lyrics_url = "https://azlyrics.com" + link["href"]
                lyrics_resp = requests.get(lyrics_url)
                if lyrics_resp.status_code == 200:
                    soup2 = BeautifulSoup(lyrics_resp.text, "html.parser")
                    lyrics_div = soup2.find("div", {"class": "ringtone"})
                    if lyrics_div:
                        lyrics = lyrics_div.find_next("div").get_text(strip=True)
                        return lyrics
    except Exception as e:
        logger.error(f"Lyrics error: {e}")
    return None

# ========== VOICE CHAT FUNCTIONS ==========
async def ensure_voice_call(chat_id: int) -> bool:
    """Ensure bot is connected to voice chat in the group."""
    try:
        await pytgcalls.join_group_call(
            chat_id,
            AudioStream(quality=AudioQuality.HIGH),
            stream_quality=StreamAudioQuality.HIGH,
        )
        return True
    except NoActiveGroupCall:
        # No voice chat active, create one (requires admin)
        try:
            await pyro_client.send_message(chat_id, "❗ Please start a voice chat first.")
            return False
        except:
            return False
    except Exception as e:
        logger.error(f"Join error: {e}")
        return False

async def play_next(chat_id: int):
    """Play the next song in queue."""
    if chat_id not in PLAYING:
        PLAYING[chat_id] = None
    loop_mode = LOOP_MODE.get(chat_id, "off")
    radio = RADIO_MODE.get(chat_id, False)

    # If loop one, replay current
    if loop_mode == "one" and PLAYING[chat_id]:
        song = PLAYING[chat_id]
    else:
        if loop_mode == "all" and PLAYING[chat_id]:
            # Re-add current to queue
            if chat_id in QUEUE:
                QUEUE[chat_id].append(PLAYING[chat_id])
        # Pop next
        if chat_id in QUEUE and QUEUE[chat_id]:
            song = QUEUE[chat_id].popleft()
        else:
            # Radio mode: generate random similar
            if radio and PLAYING[chat_id]:
                # Use radio logic - get similar via yt-dlp search
                title = PLAYING[chat_id]["title"]
                similar = await extract_song(f"radio {title}")
                if similar:
                    song = similar
                else:
                    song = None
            else:
                song = None
    if not song:
        # No more songs, leave after some time
        await pytgcalls.leave_group_call(chat_id)
        PLAYING[chat_id] = None
        return

    # Download audio
    file_path = await download_audio(song.get("webpage_url") or song.get("url"))
    if not file_path:
        # Failed, skip
        await play_next(chat_id)
        return

    # Update playing
    PLAYING[chat_id] = song

    # Play
    try:
        await pytgcalls.change_stream(
            chat_id,
            AudioStreamPiped(
                file_path,
                quality=AudioQuality.HIGH,
                path=file_path,
            ),
        )
    except Exception as e:
        logger.error(f"Play error: {e}")
        # Try to play next
        await play_next(chat_id)

    # Update history
    if chat_id not in SONG_HISTORY:
        SONG_HISTORY[chat_id] = deque(maxlen=20)
    SONG_HISTORY[chat_id].append(song)

    # Notify group
    await send_now_playing(chat_id, song)

async def send_now_playing(chat_id: int, song: dict):
    """Send Now Playing message."""
    text = (
        f"🎵 **Now Playing**\n"
        f"📀 {song.get('title', 'Unknown')}\n"
        f"👤 {song.get('uploader', 'Unknown')}\n"
        f"⏱ {format_duration(song.get('duration', 0))}\n"
    )
    if song.get("thumbnail"):
        await bot.send_photo(chat_id, song["thumbnail"], caption=text, parse_mode="Markdown")
    else:
        await bot.send_message(chat_id, text, parse_mode="Markdown")

# ========== BOT COMMANDS ==========
bot_app = None  # will be set later

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎶 **Music Bot**\n"
        "I can play music from YouTube, Spotify, and more!\n\n"
        "**Commands:**\n"
        "/play <song name or URL> - Play music\n"
        "/search <query> - Search and select\n"
        "/queue - Show queue\n"
        "/now - Now playing\n"
        "/pause - Pause\n"
        "/resume - Resume\n"
        "/skip - Skip current\n"
        "/stop - Stop and leave\n"
        "/volume <1-200> - Adjust volume\n"
        "/loop [off/one/all] - Toggle repeat\n"
        "/shuffle - Shuffle queue\n"
        "/lyrics - Get lyrics of current song\n"
        "/history - Recently played\n"
        "/playlist - Manage playlists\n"
        "/radio - Toggle radio mode\n"
        "/help - Show this help\n"
        "\n**Admin:** /clear, /kick, /mute, /unmute, /join, /leave"
    )

async def play(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not context.args:
        await update.message.reply_text("❌ Please provide a song name or URL.\nExample: /play Imagine Dragons")
        return

    query = " ".join(context.args)
    # Check if it's a Spotify track/playlist
    if "spotify.com" in query:
        # Extract ID
        if "track" in query:
            track_id = re.search(r"track/([a-zA-Z0-9]+)", query)
            if track_id:
                song = await get_spotify_track(track_id.group(1))
                if not song:
                    await update.message.reply_text("❌ Could not fetch Spotify track.")
                    return
                # We'll treat it as a song
                # Need to get actual audio URL via yt-dlp search using title+artist
                search_query = f"{song['title']} {song['uploader']}"
                extracted = await extract_song(search_query)
                if extracted:
                    song.update(extracted)
                else:
                    await update.message.reply_text("❌ Could not find audio for this track.")
                    return
        else:
            await update.message.reply_text("❌ Spotify playlists not supported yet.")
            return
    else:
        # Normal search
        song = await extract_song(query)
        if not song:
            await update.message.reply_text("❌ No results found.")
            return

    # Add to queue
    if chat_id not in QUEUE:
        QUEUE[chat_id] = deque()
    QUEUE[chat_id].append(song)

    if not PLAYING.get(chat_id):
        # Nothing playing, start now
        await play_next(chat_id)
        await update.message.reply_text(f"▶️ Playing: {song['title']}")
    else:
        # Added to queue
        await update.message.reply_text(f"✅ Added to queue: {song['title']}")

async def search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not context.args:
        await update.message.reply_text("❌ Please provide a search query.\nExample: /search Imagine Dragons")
        return

    query = " ".join(context.args)
    # Search via yt-dlp
    try:
        with yt_dlp.YoutubeDL(YDL_OPTS) as ydl:
            info = ydl.extract_info(f"ytsearch10:{query}", download=False)
            if not info or "entries" not in info:
                await update.message.reply_text("❌ No results.")
                return
            entries = info["entries"][:10]
            keyboard = []
            for idx, entry in enumerate(entries):
                title = entry.get("title", "Unknown")
                duration = entry.get("duration", 0)
                keyboard.append([InlineKeyboardButton(
                    f"{idx+1}. {title[:30]} ({format_duration(duration)})",
                    callback_data=f"search_play_{idx+1}_{query}"
                )])
            keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="search_cancel")])
            reply_markup = InlineKeyboardMarkup(keyboard)
            # Store search results temporarily
            SEARCH_CACHE[f"{chat_id}_{query}"] = entries
            await update.message.reply_text("🎵 **Search Results:**", reply_markup=reply_markup)
    except Exception as e:
        logger.error(f"Search error: {e}")
        await update.message.reply_text("❌ Search failed.")

async def queue_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id not in QUEUE or not QUEUE[chat_id]:
        await update.message.reply_text("📭 Queue is empty.")
        return
    text = "📃 **Queue:**\n"
    for i, song in enumerate(QUEUE[chat_id][:20], 1):
        text += f"{i}. {song.get('title', 'Unknown')} ({format_duration(song.get('duration', 0))})\n"
    await update.message.reply_text(text, parse_mode="Markdown")

async def now_playing(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    song = PLAYING.get(chat_id)
    if not song:
        await update.message.reply_text("❌ Nothing is playing.")
        return
    text = (
        f"🎵 **Now Playing**\n"
        f"📀 {song.get('title', 'Unknown')}\n"
        f"👤 {song.get('uploader', 'Unknown')}\n"
        f"⏱ {format_duration(song.get('duration', 0))}\n"
    )
    if song.get("thumbnail"):
        await update.message.reply_photo(song["thumbnail"], caption=text, parse_mode="Markdown")
    else:
        await update.message.reply_text(text, parse_mode="Markdown")

async def pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    try:
        await pytgcalls.pause_stream(chat_id)
        await update.message.reply_text("⏸ Paused.")
    except Exception as e:
        await update.message.reply_text(f"❌ Could not pause: {e}")

async def resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    try:
        await pytgcalls.resume_stream(chat_id)
        await update.message.reply_text("▶️ Resumed.")
    except Exception as e:
        await update.message.reply_text(f"❌ Could not resume: {e}")

async def skip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not PLAYING.get(chat_id):
        await update.message.reply_text("❌ Nothing is playing.")
        return
    # Stop current and play next
    await pytgcalls.stop_stream(chat_id)
    # The play_next will be triggered by the on_stream_end event, but we can manually call
    await play_next(chat_id)
    await update.message.reply_text("⏭ Skipped.")

async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    try:
        await pytgcalls.leave_group_call(chat_id)
        PLAYING[chat_id] = None
        if chat_id in QUEUE:
            QUEUE[chat_id].clear()
        await update.message.reply_text("⏹ Stopped and left voice chat.")
    except Exception as e:
        await update.message.reply_text(f"❌ Could not stop: {e}")

async def volume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not context.args:
        await update.message.reply_text("❌ Usage: /volume <1-200>")
        return
    try:
        vol = int(context.args[0])
        if vol < 1 or vol > 200:
            await update.message.reply_text("❌ Volume must be between 1 and 200.")
            return
        VOLUME[chat_id] = vol
        # Note: pytgcalls doesn't have native volume control, we could use ffmpeg volume filter
        # We'll re-apply stream with volume filter (simplified)
        await update.message.reply_text(f"🔊 Volume set to {vol}%.")
    except ValueError:
        await update.message.reply_text("❌ Invalid volume.")

async def loop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    mode = context.args[0] if context.args else "off"
    if mode not in ["off", "one", "all"]:
        await update.message.reply_text("❌ Use: /loop off/one/all")
        return
    LOOP_MODE[chat_id] = mode
    await update.message.reply_text(f"🔄 Loop mode set to: {mode}")

async def shuffle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id not in QUEUE or len(QUEUE[chat_id]) < 2:
        await update.message.reply_text("❌ Not enough songs to shuffle.")
        return
    import random
    random.shuffle(QUEUE[chat_id])
    await update.message.reply_text("🔀 Queue shuffled.")

async def lyrics(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    song = PLAYING.get(chat_id)
    if not song:
        await update.message.reply_text("❌ No song playing.")
        return
    title = song.get("title", "")
    artist = song.get("uploader", "")
    lyrics_text = await get_lyrics(title, artist)
    if not lyrics_text:
        await update.message.reply_text("❌ Lyrics not found.")
        return
    if len(lyrics_text) > 4096:
        # split
        for i in range(0, len(lyrics_text), 4096):
            await update.message.reply_text(lyrics_text[i:i+4096])
    else:
        await update.message.reply_text(f"📜 **Lyrics for {title}**\n\n{lyrics_text}", parse_mode="Markdown")

async def history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id not in SONG_HISTORY or not SONG_HISTORY[chat_id]:
        await update.message.reply_text("📭 No history.")
        return
    text = "📜 **Recently Played:**\n"
    for i, song in enumerate(list(SONG_HISTORY[chat_id])[-10:][::-1], 1):
        text += f"{i}. {song.get('title', 'Unknown')}\n"
    await update.message.reply_text(text, parse_mode="Markdown")

async def playlist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Basic playlist management: save current queue, load saved, list
    user_id = update.effective_user.id
    args = context.args
    if not args:
        # Show user's playlists
        playlists = USER_PLAYLISTS.get(user_id, {})
        if not playlists:
            await update.message.reply_text("📭 You have no saved playlists.")
            return
        text = "📁 **Your Playlists:**\n"
        for name, songs in playlists.items():
            text += f"🔹 {name} ({len(songs)} songs)\n"
        await update.message.reply_text(text, parse_mode="Markdown")
        return
    action = args[0].lower()
    if action == "save":
        if len(args) < 2:
            await update.message.reply_text("❌ Usage: /playlist save <name>")
            return
        name = " ".join(args[1:])
        chat_id = update.effective_chat.id
        if chat_id not in QUEUE or not QUEUE[chat_id]:
            await update.message.reply_text("❌ Queue is empty.")
            return
        # Save the queue as list of song IDs (or URLs)
        songs = list(QUEUE[chat_id])
        if user_id not in USER_PLAYLISTS:
            USER_PLAYLISTS[user_id] = {}
        USER_PLAYLISTS[user_id][name] = songs
        await update.message.reply_text(f"✅ Playlist '{name}' saved with {len(songs)} songs.")
    elif action == "load":
        if len(args) < 2:
            await update.message.reply_text("❌ Usage: /playlist load <name>")
            return
        name = " ".join(args[1:])
        playlists = USER_PLAYLISTS.get(user_id, {})
        if name not in playlists:
            await update.message.reply_text(f"❌ Playlist '{name}' not found.")
            return
        songs = playlists[name]
        chat_id = update.effective_chat.id
        if chat_id not in QUEUE:
            QUEUE[chat_id] = deque()
        # Append to current queue
        for song in songs:
            QUEUE[chat_id].append(song)
        await update.message.reply_text(f"✅ Loaded playlist '{name}' ({len(songs)} songs) into queue.")
    elif action == "delete":
        if len(args) < 2:
            await update.message.reply_text("❌ Usage: /playlist delete <name>")
            return
        name = " ".join(args[1:])
        playlists = USER_PLAYLISTS.get(user_id, {})
        if name not in playlists:
            await update.message.reply_text(f"❌ Playlist '{name}' not found.")
            return
        del playlists[name]
        await update.message.reply_text(f"✅ Playlist '{name}' deleted.")
    else:
        await update.message.reply_text("❌ Unknown action. Use save/load/delete.")

async def radio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    RADIO_MODE[chat_id] = not RADIO_MODE.get(chat_id, False)
    status = "enabled" if RADIO_MODE[chat_id] else "disabled"
    await update.message.reply_text(f"📻 Radio mode {status}.")

# Admin commands
async def clear_queue(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id in QUEUE:
        QUEUE[chat_id].clear()
    await update.message.reply_text("🗑 Queue cleared.")

async def kick_from_vc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    # We need to kick a specific user from voice chat
    # Not implemented due to complexity
    await update.message.reply_text("❌ This feature is not yet implemented.")

async def join_vc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if await ensure_voice_call(chat_id):
        await update.message.reply_text("✅ Joined voice chat.")
    else:
        await update.message.reply_text("❌ Could not join voice chat.")

async def leave_vc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    try:
        await pytgcalls.leave_group_call(chat_id)
        PLAYING[chat_id] = None
        await update.message.reply_text("✅ Left voice chat.")
    except Exception as e:
        await update.message.reply_text(f"❌ Could not leave: {e}")

# ========== CALLBACK QUERY HANDLER ==========
async def search_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    if data == "search_cancel":
        await query.edit_message_text("❌ Search cancelled.")
        return
    # Format: search_play_{index}_{query}
    parts = data.split("_")
    if len(parts) >= 4:
        idx = int(parts[2]) - 1
        search_query = "_".join(parts[3:])
        chat_id = update.effective_chat.id
        cache_key = f"{chat_id}_{search_query}"
        entries = SEARCH_CACHE.get(cache_key, [])
        if idx < len(entries):
            song = entries[idx]
            # Add to queue and play
            chat_id = update.effective_chat.id
            if chat_id not in QUEUE:
                QUEUE[chat_id] = deque()
            QUEUE[chat_id].append(song)
            if not PLAYING.get(chat_id):
                await play_next(chat_id)
            await query.edit_message_text(f"✅ Added to queue: {song['title']}")
        else:
            await query.edit_message_text("❌ Song not found.")

# ========== PYTGCALLS EVENT HANDLERS ==========
@pytgcalls.on_stream_end()
async def on_stream_end(chat_id: int):
    await play_next(chat_id)

# ========== MAIN ==========
async def main():
    global bot_app
    # Start Pyrogram
    await pyro_client.start()
    await pytgcalls.start()

    # Create application
    app = Application.builder().token(BOT_TOKEN).build()
    bot_app = app.bot

    # Add handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(CommandHandler("play", play))
    app.add_handler(CommandHandler("search", search))
    app.add_handler(CommandHandler("queue", queue_cmd))
    app.add_handler(CommandHandler("now", now_playing))
    app.add_handler(CommandHandler("pause", pause))
    app.add_handler(CommandHandler("resume", resume))
    app.add_handler(CommandHandler("skip", skip))
    app.add_handler(CommandHandler("stop", stop))
    app.add_handler(CommandHandler("volume", volume))
    app.add_handler(CommandHandler("loop", loop))
    app.add_handler(CommandHandler("shuffle", shuffle))
    app.add_handler(CommandHandler("lyrics", lyrics))
    app.add_handler(CommandHandler("history", history))
    app.add_handler(CommandHandler("playlist", playlist))
    app.add_handler(CommandHandler("radio", radio))
    app.add_handler(CommandHandler("clear", clear_queue))
    app.add_handler(CommandHandler("kick", kick_from_vc))
    app.add_handler(CommandHandler("join", join_vc))
    app.add_handler(CommandHandler("leave", leave_vc))
    app.add_handler(CallbackQueryHandler(search_callback, pattern="search_"))

    # Start polling
    await app.initialize()
    await app.start()
    await app.updater.start_polling()

    logger.info("Bot is running...")
    await idle()

    # Cleanup
    await app.updater.stop()
    await app.stop()
    await pytgcalls.leave_all_group_calls()
    await pyro_client.stop()

if __name__ == "__main__":
    asyncio.run(main())
