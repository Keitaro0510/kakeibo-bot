import gspread
from oauth2client.service_account import ServiceAccountCredentials
from flask import Flask, request, abort, send_from_directory
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage, ImageSendMessage
from openai import OpenAI
import datetime
from datetime import datetime
import matplotlib
matplotlib.use('Agg') # サーバー（Render）でグラフを描画するための設定
import matplotlib.pyplot as plt
import japanize_matplotlib
import io
import os
import json
from dotenv import load_dotenv

app = Flask(__name__)
load_dotenv()

# 静的ファイル（グラフ画像）を外部から見れるようにする設定
@app.route('/static/<path:filename>')
def serve_static(filename):
    return send_from_directory('static', filename)

# --- 各種設定 ---
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

# --- 便利関数：AIが混ぜてくる「項目:」などのラベルや「円」を消して中身だけにする ---
def clean_val(text):
    text = str(text)
    if ":" in text: text = text.split(":")[-1]
    if "：" in text: text = text.split("：")[-1]
    return text.replace('円', '').replace(',', '').strip()

# --- グラフ作成関数 ---
def create_pie_chart(data):
    category_totals = {}
    for record in data:
        cat = clean_val(record.get('カテゴリ', 'その他'))
        try:
            amt = int(clean_val(record.get('金額', 0)))
            if amt > 0:
                category_totals[cat] = category_totals.get(cat, 0) + amt
        except: continue

    if not category_totals: return None

    plt.figure(figsize=(6, 6))
    plt.pie(list(category_totals.values()), labels=list(category_totals.keys()), autopct='%1.1f%%', startangle=140, shadow=True)
    plt.title("今月の支出内訳")

    buf = io.BytesIO()
    plt.savefig(buf, format='png')
    buf.seek(0)
    plt.close()
    return buf

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    try:
        user_message = event.message.text
        today = datetime.now()

        # --- AIによる意図解析 ---
        intent_res = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": f"意図(record, total, graph)を分析し「意図,キーワード,期間」で返して。余計な説明は不要：{user_message}"}]
        )
        intent_raw = intent_res.choices[0].message.content.strip().split('\n')[-1]
        intent_data = intent_raw.split(',')
        intent = intent_data[0].strip()

        # 強制グラフ判定
        if "graph" in intent or "グラフ" in user_message:
            intent = "graph"

        # --- グラフ表示 ---
        if intent == "graph":
            all_records = sheet.get_all_records()
            this_month_data = []
            for r in all_records:
                try:
                    rec_date = datetime.strptime(str(r['日付']), '%Y/%m/%d')
                    if rec_date.year == today.year and rec_date.month == today.month:
                        this_month_data.append(r)
                except: continue

            chart_buf = create_pie_chart(this_month_data)
            if chart_buf:
                # staticフォルダの安全確認（ファイルなら消してフォルダ化）
                if os.path.exists("static") and not os.path.isdir("static"):
                    os.remove("static")
                os.makedirs("static", exist_ok=True)
                
                filepath = os.path.join("static", "graph.png")
                with open(filepath, "wb") as f:
                    f.write(chart_buf.getbuffer())

                # 画像URLの生成（Renderのドメインを自動取得）
                image_url = f"https://{request.host}/static/graph.png?{int(today.timestamp())}"
                line_bot_api.reply_message(
                    event.reply_token, 
                    ImageSendMessage(original_content_url=image_url, preview_image_url=image_url)
                )
            else:
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text="今月のデータがないよ"))
            return

        # --- 合計計算 ---
        elif intent == "total":
            all_records = sheet.get_all_records()
            total = 0
            for r in all_records:
                try:
                    rec_date = datetime.strptime(str(r['日付']), '%Y/%m/%d')
                    if rec_date.year == today.year and rec_date.month == today.month:
                        total += int(clean_val(r.get('金額', 0)))
                except: continue
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"📊 今月の合計は {total:,}円 だよ！"))

        # --- 支出記録（デフォルト） ---
        else:
            ai_res = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": f"必ず「項目,カテゴリ,金額」の値だけをカンマ区切りで返して。ラベルや説明は一切不要：{user_message}"}]
            )
            ai_data = ai_res.choices[0].message.content.strip().split('\n')[-1]
            parts = ai_data.split(',')
            
            # データのクリーニング（「項目:」などを除去）
            item = clean_val(parts[0])
            category = clean_val(parts[1]) if len(parts) > 1 else "その他"
            amount = clean_val(parts[2]) if len(parts) > 2 else "0"
            
            today_str = today.strftime('%Y/%m/%d')
            sheet.append_row([today_str, item, category, amount])
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"✅ 記録したよ！\n{item} ({category}): {amount}円"))

    except Exception as e:
        # エラーが起きたらLINEに通知
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"エラーが発生しました：{str(e)}"))

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    try: handler.handle(body, signature)
    except InvalidSignatureError: abort(400)
    return 'OK'

if __name__ == "__main__":
    # Render環境に合わせてポートとホストを設定
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
