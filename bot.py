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

# ─────────────────────────────────────────
# 設定
# ─────────────────────────────────────────
DISCORD_TOKEN   = os.environ["DISCORD_TOKEN"]
GEMINI_KEY      = os.environ["GEMINI_API_KEY"]

WATCH_CHANNELS  = ["授業連絡", "課題", "web3ai概論", "general", "アナウンス"]
SUMMARY_CHANNEL = "課題まとめ"
LOOKBACK_HOURS  = 48
INTERVAL_HOURS  = 6

# ─────────────────────────────────────────
# Discordクライアント / Geminiクライアント
# ─────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

ai = genai.Client(api_key=GEMINI_KEY)

# ─────────────────────────────────────────
# AIによる課題抽出
# ─────────────────────────────────────────
def extract_tasks_with_ai(messages: list[dict]) -> list[dict]:
    if not messages:
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

    response = ai.models.generate_content(
        model="gemini-2.0-flash",
        contents=prompt
    )
    raw = response.text.strip()

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

        prefix = "🚨 " if urgent else "• "
        lines.append(f"{prefix}**{title}**\n　{dl_label}　{channel}")

    embed.description = "\n\n".join(lines)
    return embed

# ─────────────────────────────────────────
# メイン巡回処理
# ─────────────────────────────────────────
async def scan_and_post():
    print(f"[{datetime.now():%H:%M}] 巡回開始...")

    since        = datetime.now() - timedelta(hours=LOOKBACK_HOURS)
    all_messages = []

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

    tasks = extract_tasks_with_ai(all_messages)
    print(f"  {len(tasks)} 件の課題を検出")

    for guild in client.guilds:
        summary_ch = discord.utils.get(guild.text_channels, name=SUMMARY_CHANNEL)
        if summary_ch is None:
            print(f"  ⚠️ #{SUMMARY_CHANNEL} チャンネルが見つかりません。Discordで手動作成してください。")
            continue

        embed = build_summary_embed(tasks)
        await summary_ch.send(embed=embed)
        print(f"  ✅ #{SUMMARY_CHANNEL} に投稿しました")

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
client.run(DISCORD_TOKEN)
