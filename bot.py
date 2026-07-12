"""
課題自動収集Discordボット（Gemini版）
- 指定チャンネルを定期巡回し、AIが課題・締切を自動検出
- 結果を #課題まとめ チャンネルに投稿
"""

import discord
from google import genai
import asyncio
import json
import os
from datetime import datetime, timedelta
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from manaba_scraper import get_manaba_tasks
import sys
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from contextlib import asynccontextmanager
from pydantic import BaseModel

# Windows環境でのUTF-8出力対応
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
if hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

# .envファイルの読み込み
load_dotenv()

# ─────────────────────────────────────────
# 設定
# ─────────────────────────────────────────
DISCORD_TOKEN   = os.environ.get("DISCORD_TOKEN")
GEMINI_KEY      = os.environ.get("GEMINI_API_KEY")

WATCH_CHANNELS  = ["授業連絡", "課題", "web3ai概論", "general", "アナウンス"]
SUMMARY_CHANNEL = "課題まとめ"
LOOKBACK_HOURS  = 48
INTERVAL_HOURS  = 6

# ─────────────────────────────────────────
# Discordクライアント
# ─────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

# ─────────────────────────────────────────
# AIによる課題抽出
# ─────────────────────────────────────────
def extract_tasks_with_ai(messages: list[dict]) -> list[dict]:
    if not messages:
        return []

    if not GEMINI_KEY:
        print("  ⚠️ GEMINI_API_KEY が未設定のため、AIによる課題抽出をスキップします。")
        return []

    text = "\n".join(
        f"[#{m['channel']}] {m['author']}: {m['content']}"
        for m in messages
    )

    prompt = f"""以下はDiscordのメッセージ一覧です。
課題・宿題・提出物・締切に関する情報を全て抽出してください。

ルール:
- 課題でないメッセージは無視する
- deadlineはYYYY-MM-DD形式。不明な場合は空文字
- urgentは締切が3日以内またはメッセージに「急ぎ」「今日」「明日」などがある場合はtrue
- 同じ課題が複数チャンネルに出ていたら1件にまとめる
- JSON配列のみ返す。説明文は不要

出力形式:
[
  {{"title": "課題名", "deadline": "2026-06-02", "channel": "#チャンネル名", "urgent": false}}
]

メッセージ一覧:
{text}
"""

    try:
        ai = genai.Client(api_key=GEMINI_KEY)
        response = ai.models.generate_content(
            model="gemini-2.0-flash",
            contents=prompt
        )
        raw = response.text.strip()
    except Exception as e:
        print(f"  ⚠️ Gemini APIでエラーが発生しました: {e}")
        return []

    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    raw = raw.strip()

    try:
        tasks = json.loads(raw)
        return tasks if isinstance(tasks, list) else []
    except json.JSONDecodeError:
        return []

# ─────────────────────────────────────────
# 課題まとめEmbedを作成
# ─────────────────────────────────────────
def build_summary_embed(tasks: list[dict]) -> discord.Embed:
    today = datetime.now().date()

    embed = discord.Embed(
        title="📋 課題まとめ（自動更新）",
        color=0x5865F2,
        timestamp=datetime.now()
    )
    embed.set_footer(text="AIが自動検出 • 次回更新まで約6時間")

    if not tasks:
        embed.description = "現在検出された課題はありません ✅"
        return embed

    tasks_sorted = sorted(tasks, key=lambda t: t.get("deadline", "") or "9999-99-99")

    lines = []
    for t in tasks_sorted:
        deadline = t.get("deadline", "")
        title    = t.get("title", "不明な課題")
        channel  = t.get("channel", "")
        urgent   = t.get("urgent", False)
        source   = t.get("source", "discord")

        if deadline:
            d    = datetime.strptime(deadline, "%Y-%m-%d").date()
            diff = (d - today).days
            if diff < 0:
                dl_label = f"⚠️ 期限切れ ({deadline})"
            elif diff == 0:
                dl_label = f"🔴 **今日** ({deadline})"
            elif diff <= 3:
                dl_label = f"🟠 {diff}日後 ({deadline})"
            else:
                dl_label = f"🟢 {deadline}"
        else:
            dl_label = "📅 締切不明"

        source_label = "📚 manaba" if source == "manaba" else "💬 Discord"
        prefix = "🚨 " if urgent else "• "
        lines.append(f"{prefix}**{title}**\n　{dl_label}　{channel}　`{source_label}`")

    embed.description = "\n\n".join(lines)
    return embed

# ─────────────────────────────────────────
# メイン巡回処理
# ─────────────────────────────────────────
async def scan_and_post():
    if state.is_scanning:
        print("  ⚠️ 既に巡回処理が実行中ですのでスキップします。")
        return load_saved_tasks()

    state.is_scanning = True
    state.last_scan_status = "Scanning..."
    print(f"[{datetime.now():%H:%M}] 巡回開始...")

    try:
        since        = datetime.now() - timedelta(hours=LOOKBACK_HOURS)
        all_messages = []

        if state.discord_configured and client.is_ready():
            for guild in client.guilds:
                for channel in guild.text_channels:
                    if channel.name not in WATCH_CHANNELS:
                        continue
                    try:
                        async for msg in channel.history(after=since, limit=200):
                            if msg.author.bot:
                                continue
                            all_messages.append({
                                "channel": channel.name,
                                "author":  str(msg.author.display_name),
                                "content": msg.content[:500],
                            })
                    except discord.Forbidden:
                        print(f"  アクセス権なし: #{channel.name}")
            print(f"  {len(all_messages)} 件のメッセージを取得")
        else:
            print("  ⚠️ Discord未接続のため、Discordメッセージの巡回をスキップします。")

        discord_tasks = extract_tasks_with_ai(all_messages)
        for t in discord_tasks:
            t["source"] = "discord"
        if discord_tasks:
            print(f"  Discord: {len(discord_tasks)} 件の課題を検出")

        # ── manaba スクレイピング ──────────────────────────────────
        manaba_tasks   = []
        manaba_id      = os.environ.get("MANABA_ID")
        manaba_pw      = os.environ.get("MANABA_PASSWORD")
        login_failed   = False

        if manaba_id and manaba_pw:
            try:
                loop         = asyncio.get_event_loop()
                manaba_tasks = await loop.run_in_executor(
                    None, get_manaba_tasks, manaba_id, manaba_pw
                )
                print(f"  manaba: {len(manaba_tasks)} 件の課題を取得")
            except ValueError as e:
                if "login_failed" in str(e):
                    login_failed = True
                    print("  ⚠️ manabaログイン失敗")
                else:
                    print(f"  manaba取得エラー: {e}")
            except Exception as e:
                print(f"  manaba取得エラー: {e}")
        else:
            print("  MANABA_ID/MANABA_PASSWORD 未設定 → manabaスクレイピングをスキップ")

        tasks = discord_tasks + manaba_tasks
        print(f"  合計 {len(tasks)} 件の課題")

        # 課題の保存
        save_tasks(tasks)
        state.last_scan_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        state.last_scan_status = "Success"
        state.manaba_login_failed = login_failed

        if state.discord_configured and client.is_ready():
            for guild in client.guilds:
                # ログイン失敗通知（課題投稿とは別に送る）
                if login_failed:
                    summary_ch = discord.utils.get(guild.text_channels, name=SUMMARY_CHANNEL)
                    if summary_ch:
                        await summary_ch.send(
                            "⚠️ manabaログイン失敗：`MANABA_ID` と `MANABA_PASSWORD` を確認してください"
                        )

            for guild in client.guilds:
                summary_ch = discord.utils.get(guild.text_channels, name=SUMMARY_CHANNEL)
                if summary_ch is None:
                    print(f"  ⚠️ #{SUMMARY_CHANNEL} チャンネルが見つかりません。Discordで手動作成してください。")
                    continue

                embed = build_summary_embed(tasks)
                await summary_ch.send(embed=embed)
                print(f"  ✅ #{SUMMARY_CHANNEL} に投稿しました")

        return tasks

    except Exception as e:
        state.last_scan_status = f"Failed: {str(e)}"
        print(f"  ⚠️ 巡回処理でエラーが発生しました: {e}")
        raise e
    finally:
        state.is_scanning = False

# ─────────────────────────────────────────
# Discordイベント
# ─────────────────────────────────────────
@client.event
async def on_ready():
    print(f"✅ ログイン成功: {client.user}")
    print(f"   監視チャンネル: {WATCH_CHANNELS}")
    print(f"   投稿先: #{SUMMARY_CHANNEL}")

    await scan_and_post()

    scheduler = AsyncIOScheduler()
    scheduler.add_job(scan_and_post, "interval", hours=INTERVAL_HOURS)
    scheduler.start()
    print(f"   {INTERVAL_HOURS}時間ごとに自動巡回します")

@client.event
async def on_message(message):
    if message.author.bot:
        return
    if message.content.strip() == "!scan":
        await message.channel.send("🔍 手動巡回を開始します...")
        await scan_and_post()

# ─────────────────────────────────────────
# 起動
# ─────────────────────────────────────────
# ─────────────────────────────────────────
# Web API & 状態管理
# ─────────────────────────────────────────
class BotState:
    def __init__(self):
        self.last_scan_time = None
        self.is_scanning = False
        self.last_scan_status = "Not run yet"
        self.manaba_configured = False
        self.discord_configured = False
        self.gemini_configured = False
        self.manaba_login_failed = False

state = BotState()

TASKS_FILE = "tasks.json"

def load_saved_tasks() -> list[dict]:
    if os.path.exists(TASKS_FILE):
        try:
            with open(TASKS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"tasks.json読み込みエラー: {e}")
    return []

def save_tasks(tasks: list[dict]):
    try:
        with open(TASKS_FILE, "w", encoding="utf-8") as f:
            json.dump(tasks, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"tasks.json書き込みエラー: {e}")

async def start_discord_bot():
    if not DISCORD_TOKEN:
        print("⚠️ DISCORD_TOKEN が設定されていないため、Discordボットの接続をスキップします。")
        return
    try:
        await client.start(DISCORD_TOKEN)
    except Exception as e:
        print(f"❌ Discordボットのログインに失敗しました: {e}")
        state.discord_configured = False

@asynccontextmanager
async def lifespan(app: FastAPI):
    # 設定状況の確認
    state.discord_configured = bool(DISCORD_TOKEN)
    state.gemini_configured = bool(GEMINI_KEY)
    state.manaba_configured = bool(os.environ.get("MANABA_ID") and os.environ.get("MANABA_PASSWORD"))

    # 起動時にローカルから保存済み課題を読み込み
    saved = load_saved_tasks()
    print(f"起動時に {len(saved)} 件の課題を {TASKS_FILE} から読み込みました。")

    # バックグラウンドでDiscordボットを起動
    if state.discord_configured:
        print("バックグラウンドでDiscordボットを起動中...")
        asyncio.create_task(start_discord_bot())
    else:
        print("⚠️ DISCORD_TOKEN 未設定のため、Discord機能は無効化されています。")

    yield

    # シャットダウン時の処理
    if state.discord_configured:
        print("Discordボットを停止中...")
        await client.close()

# ─────────────────────────────────────────
# 設定のロード・セーブ処理
# ─────────────────────────────────────────
def mask_token(token: str) -> str:
    if not token:
        return ""
    if len(token) <= 8:
        return "****"
    return f"{token[:4]}...{token[-4:]}"

def save_env_values(values: dict):
    env_path = ".env"
    existing = {}
    
    # 既存の .env があれば読み込む
    if os.path.exists(env_path):
        try:
            with open(env_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        parts = line.split("=", 1)
                        if len(parts) == 2:
                            existing[parts[0].strip()] = parts[1].strip()
        except Exception as e:
            print(f"Error reading .env: {e}")
            
    # 新しい値で更新
    for k, v in values.items():
        existing[k] = v
        
    # 保存
    try:
        with open(env_path, "w", encoding="utf-8") as f:
            f.write("# Auto-generated configuration by Task-Bot Dashboard\n")
            for k, v in existing.items():
                f.write(f"{k}={v}\n")
    except Exception as e:
        print(f"Error writing to .env: {e}")

class SettingsUpdate(BaseModel):
    DISCORD_TOKEN: str | None = None
    GEMINI_API_KEY: str | None = None
    MANABA_ID: str | None = None
    MANABA_PASSWORD: str | None = None

app = FastAPI(lifespan=lifespan)

@app.get("/", response_class=HTMLResponse)
def read_dashboard():
    try:
        static_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "index.html")
        with open(static_path, "r", encoding="utf-8") as f:
            return HTMLResponse(content=f.read())
    except FileNotFoundError:
        return HTMLResponse(content="<h1>Dashboard HTML template not found in static/index.html</h1>", status_code=404)

@app.get("/api/status")
def get_status():
    discord_active = client.is_ready() if state.discord_configured else False
    return {
        "discord_configured": state.discord_configured,
        "discord_active": discord_active,
        "gemini_configured": state.gemini_configured,
        "manaba_configured": state.manaba_configured,
        "manaba_login_failed": state.manaba_login_failed,
        "is_scanning": state.is_scanning,
        "last_scan_time": state.last_scan_time,
        "last_scan_status": state.last_scan_status,
    }

@app.get("/api/tasks")
def get_tasks():
    return load_saved_tasks()

@app.post("/api/scan")
async def trigger_scan():
    if state.is_scanning:
        raise HTTPException(status_code=400, detail="Scan already in progress")
    try:
        tasks = await scan_and_post()
        return {"status": "success", "tasks": tasks}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/settings")
def get_settings():
    return {
        "DISCORD_TOKEN": mask_token(DISCORD_TOKEN),
        "GEMINI_API_KEY": mask_token(GEMINI_KEY),
        "MANABA_ID": os.environ.get("MANABA_ID", ""),
        "MANABA_PASSWORD": mask_token(os.environ.get("MANABA_PASSWORD", ""))
    }

@app.post("/api/settings")
async def update_settings(payload: SettingsUpdate):
    global DISCORD_TOKEN, GEMINI_KEY
    
    new_env = {}
    discord_updated = False
    
    # Discord Token
    if payload.DISCORD_TOKEN is not None:
        val = payload.DISCORD_TOKEN.strip()
        if val and "..." not in val:
            DISCORD_TOKEN = val
            new_env["DISCORD_TOKEN"] = val
            discord_updated = True
        elif not val:
            DISCORD_TOKEN = ""
            new_env["DISCORD_TOKEN"] = ""
            discord_updated = True
            
    # Gemini Key
    if payload.GEMINI_API_KEY is not None:
        val = payload.GEMINI_API_KEY.strip()
        if val and "..." not in val:
            GEMINI_KEY = val
            os.environ["GEMINI_API_KEY"] = val
            new_env["GEMINI_API_KEY"] = val
        elif not val:
            GEMINI_KEY = ""
            os.environ["GEMINI_API_KEY"] = ""
            new_env["GEMINI_API_KEY"] = ""
            
    # manaba ID
    if payload.MANABA_ID is not None:
        val = payload.MANABA_ID.strip()
        os.environ["MANABA_ID"] = val
        new_env["MANABA_ID"] = val
        
    # manaba Password
    if payload.MANABA_PASSWORD is not None:
        val = payload.MANABA_PASSWORD.strip()
        if val and "..." not in val:
            os.environ["MANABA_PASSWORD"] = val
            new_env["MANABA_PASSWORD"] = val
        elif not val:
            os.environ["MANABA_PASSWORD"] = ""
            new_env["MANABA_PASSWORD"] = ""
            
    if new_env:
        save_env_values(new_env)
        
    # 状態の更新
    state.discord_configured = bool(DISCORD_TOKEN)
    state.gemini_configured = bool(GEMINI_KEY)
    state.manaba_configured = bool(os.environ.get("MANABA_ID") and os.environ.get("MANABA_PASSWORD"))
    
    if discord_updated:
        # 既存クライアントの停止
        if client.is_ready():
            print("再設定のため動作中のDiscordクライアントを停止しています...")
            await client.close()
            await asyncio.sleep(1)
            
        # 再接続処理
        if state.discord_configured:
            print("新しいトークンでDiscordクライアントを起動しています...")
            asyncio.create_task(start_discord_bot())
            
    return {"status": "success", "message": "Settings updated"}

if __name__ == "__main__":
    import uvicorn
    # Webサーバーの起動（ホストを0.0.0.0に変更して外部デバイスからの接続を許可）
    uvicorn.run("bot:app", host="0.0.0.0", port=8000, reload=True)

