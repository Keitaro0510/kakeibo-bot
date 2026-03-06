import gspread
from oauth2client.service_account import ServiceAccountCredentials
from flask import Flask, request, abort, send_from_directory
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage, ImageSendMessage
from openai import OpenAI
import datetime
from datetime import datetime, timedelta
import matplotlib
matplotlib.use('Agg') 
import matplotlib.pyplot as plt
import japanize_matplotlib
import io
import os
import json
from dotenv import load_dotenv

app = Flask(__name__)
load_dotenv()

@app.route('/static/<path:filename>')
def serve_static(filename):
    return send_from_directory('static', filename)

line_bot_api = LineBotApi(os.getenv('LINE_CHANNEL_ACCESS_TOKEN'))
handler = WebhookHandler(os.getenv('LINE_CHANNEL_SECRET'))
client = OpenAI(api_key=os.getenv('OPENAI_API_KEY'))

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
            val = str(record.get('金額', 0)).replace('円', '').replace(',', '').strip()
            amt = int(val)
            category_totals[cat] = category_totals.get(cat, 0) + amt
        except: continue

    if not category_totals: return None

    plt.figure(figsize=(6, 6))
    plt.pie(list(category_totals.values()), labels=list(category_totals.keys()), autopct='%1.1f%%', startangle=140)
    plt.title("今月の支出内訳")

    buf = io.BytesIO()
    plt.savefig(buf, format='png')
    buf.seek(0)
    plt.close()
    return buf

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    # --- エラーが起きてもLINEに「エラーだよ」と送るためのtry ---
    try:
        user_message = event.message.text
        today = datetime.now()

        intent_prompt = f"意図(record, total, graph)を分析し「意図,キーワード,期間」で返して：{user_message}"
        intent_res = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": intent_prompt}]
        )
        intent_raw = intent_res.choices[0].message.content.strip().split('\n')[-1]
        intent_data = intent_raw.split(',')
        intent = intent_data[0].strip()

        if "graph" in intent or "グラフ" in user_message or "内訳" in user_message:
            intent = "graph"

        keyword = intent_data[1].strip() if len(intent_data) > 1 else "なし"
        period = intent_data[2].strip() if len(intent_data) > 2 else "なし"

        if intent == "graph":
            all_records = sheet.get_all_records()
            this_month_data = [r for r in all_records if datetime.strptime(r['日付'], '%Y/%m/%d').month == today.month]
            
            chart_buf = create_pie_chart(this_month_data)
            if chart_buf:
                if not os.path.exists("static"): os.makedirs("static")
                filepath = os.path.join("static", "graph.png")
                with open(filepath, "wb") as f:
                    f.write(chart_buf.getbuffer())

                # URL生成（Render用）
                base_url = f"https://{request.host}/" 
                image_url = f"{base_url}static/graph.png?{int(today.timestamp())}"

                line_bot_api.reply_message(
                    event.reply_token, 
                    ImageSendMessage(original_content_url=image_url, preview_image_url=image_url)
                )
            else:
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text="データがないよ"))
            return

        elif intent == "total":
            all_records = sheet.get_all_records()
            total = sum(int(str(r.get('金額', 0)).replace('円','').replace(',','')) for r in all_records if datetime.strptime(r['日付'], '%Y/%m/%d').month == today.month)
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"📊 合計は {total:,}円 だよ！"))
            return

        else:
            # 記録処理（ここは成功済みなのでそのまま）
            ai_data = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": f"「項目,カテゴリ,金額」で抽出して：{user_message}"}]
            ).choices[0].message.content.strip().split('\n')[-1]
            item, category, amt_str = ai_data.split(',')
            amt = amt_str.replace('円', '').replace(',', '').strip()
            sheet.append_row([today.strftime('%Y/%m/%d'), item, category, amt])
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"✅ 記録したよ！\n{item}: {amt}円"))

    except Exception as e:
        # 何かエラーが起きたらその内容をLINEに送る
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"エラーが発生しました：{str(e)}"))

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    try: handler.handle(body, signature)
    except InvalidSignatureError: abort(400)
    return 'OK'

# --- ここが最重要！ Render対応の起動設定 ---
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
