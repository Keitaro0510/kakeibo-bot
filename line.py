import gspread
from oauth2client.service_account import ServiceAccountCredentials
from flask import Flask, request, abort, send_from_directory # send_from_directoryを追加
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage, ImageSendMessage # ImageSendMessageを追加
from openai import OpenAI
import datetime
from datetime import datetime, timedelta
import matplotlib.pyplot as plt
import japanize_matplotlib
import io
import os
import json
from dotenv import load_dotenv

app = Flask(__name__)
load_dotenv()

# --- 【追加】静的ファイル（画像）を外部から見れるようにする設定 ---
@app.route('/static/<path:filename>')
def serve_static(filename):
    return send_from_directory('static', filename)

# --- 設定 ---
line_bot_api = LineBotApi(os.getenv('LINE_CHANNEL_ACCESS_TOKEN'))
handler = WebhookHandler(os.getenv('LINE_CHANNEL_SECRET'))
client = OpenAI(api_key=os.getenv('OPENAI_API_KEY'))

# Googleスプレッドシートの設定
scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
env_creds = os.getenv('GOOGLE_CREDENTIALS_JSON')
if env_creds:
    creds_dict = json.loads(env_creds)
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
else:
    creds = ServiceAccountCredentials.from_json_keyfile_name('google_key.json', scope)

gs_client = gspread.authorize(creds)
sheet = gs_client.open("家計簿くんデータ").sheet1 

def create_pie_chart(data):
    category_totals = {}
    for record in data:
        cat = record.get('カテゴリ', 'その他')
        try:
            amt = int(record.get('金額', 0))
            category_totals[cat] = category_totals.get(cat, 0) + amt
        except: continue

    if not category_totals: return None

    labels = list(category_totals.keys())
    values = list(category_totals.values())
    plt.figure(figsize=(6, 6))
    plt.pie(values, labels=labels, autopct='%1.1f%%', startangle=140, shadow=True)
    plt.title("今月の支出内訳")

    buf = io.BytesIO()
    plt.savefig(buf, format='png')
    buf.seek(0)
    plt.close()
    return buf

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_message = event.message.text
    today = datetime.now()

    # --- AIによる意図解析 ---
    intent_prompt = f"""
    以下のメッセージの意図を分析し、結果を必ず「意図,キーワード,期間」の形式で1行で返して。
    - record: 支出を記録したい場合
    - total: 合計を知りたい場合
    - graph: 支出のグラフを見たい場合
    【期間】this_month, last_month, this_week, all
    メッセージ: {user_message}
    """
    intent_res = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": intent_prompt}]
    )
    intent_data = intent_res.choices[0].message.content.strip().split(',')
    intent = intent_data[0].strip()
    keyword = intent_data[1].strip() if len(intent_data) > 1 else "なし"
    period = intent_data[2].strip() if len(intent_data) > 2 else "なし"

    # --- グラフ(graph)の処理 ---
    if intent == "graph":
        all_records = sheet.get_all_records()
        this_month_data = []
        for record in all_records:
            try:
                rec_date = datetime.strptime(record['日付'], '%Y/%m/%d')
                if rec_date.year == today.year and rec_date.month == today.month:
                    this_month_data.append(record)
            except: continue

        chart_buf = create_pie_chart(this_month_data)
        if chart_buf:
            # --- 【重要】サーバー内の static フォルダに画像を保存する ---
            filename = "graph.png"
            if not os.path.exists("static"): os.makedirs("static")
            filepath = os.path.join("static", filename)
            with open(filepath, "wb") as f:
                f.write(chart_buf.getbuffer())

            # 自分のサーバーのURLを生成（https://〜/static/graph.png）
            base_url = request.host_url.replace("http://", "https://")
            # LINEのキャッシュ対策でURLの末尾に時刻をつける
            image_url = f"{base_url}static/{filename}?{int(today.timestamp())}"

            line_bot_api.reply_message(
                event.reply_token, 
                ImageSendMessage(original_content_url=image_url, preview_image_url=image_url)
            )
        else:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="データがないよ"))
        return

    # --- 合計(total)の処理 ---
    elif intent == "total":
        all_records = sheet.get_all_records()
        total = 0
        match_count = 0
        for record in all_records:
            try:
                rec_date = datetime.strptime(record['日付'], '%Y/%m/%d')
                if period == "this_month" and not (rec_date.year == today.year and rec_date.month == today.month): continue
                if keyword != "なし" and (keyword not in str(record.get('項目','')) and keyword not in str(record.get('カテゴリ',''))): continue
                total += int(record['金額'])
                match_count += 1
            except: continue
        reply_text = f"📊 合計は {total:,}円 だよ！({match_count}件)"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))
        return

    # --- 記録(record)の処理 ---
    else:
        prompt = f"「項目,カテゴリ,金額」の形式で抽出して。カテゴリは（食費、日用品、交際費、交通費、その他）から選択：{user_message}"
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}]
        )
        ai_data = response.choices[0].message.content.strip()
        try:
            item, category, amount = ai_data.split(',')
            today_str = today.strftime('%Y/%m/%d')
            sheet.append_row([today_str, item, category, amount])
            reply_text = f"✅ 記録したよ！\n日付: {today_str}\n項目: {item}\nカテゴリ: {category}\n金額: {amount}円"
        except:
            reply_text = f"エラー：{ai_data}"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    try: handler.handle(body, signature)
    except InvalidSignatureError: abort(400)
    return 'OK'

if __name__ == "__main__":
    app.run(port=5000)
