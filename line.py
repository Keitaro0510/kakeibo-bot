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
matplotlib.use('Agg') # サーバーでグラフを描くための設定を追加
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
            # 金額に「円」や「,」が混じっていても数字に変換
            val = str(record.get('金額', 0)).replace('円', '').replace(',', '').strip()
            amt = int(val)
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

    intent_prompt = f"意図(record, total, graph)を分析し「意図,キーワード,期間」で返して：{user_message}"
    intent_res = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": intent_prompt}]
    )
    # AIの回答の「最後の1行」だけを使う（見出し対策）
    intent_raw = intent_res.choices[0].message.content.strip().split('\n')[-1]
    intent_data = intent_raw.split(',')
    intent = intent_data[0].strip()

    if "graph" in intent or "グラフ" in user_message or "内訳" in user_message:
        intent = "graph"

    keyword = intent_data[1].strip() if len(intent_data) > 1 else "なし"
    period = intent_data[2].strip() if len(intent_data) > 2 else "なし"

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
            filename = "graph.png"
            if not os.path.exists("static"): os.makedirs("static")
            filepath = os.path.join("static", filename)
            with open(filepath, "wb") as f:
                f.write(chart_buf.getbuffer())

            base_url = request.host_url.replace("http://", "https://")
            image_url = f"{base_url}static/{filename}?{int(today.timestamp())}"

            line_bot_api.reply_message(
                event.reply_token, 
                ImageSendMessage(original_content_url=image_url, preview_image_url=image_url)
            )
        else:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="データがないよ"))
        return

    elif intent == "total":
        all_records = sheet.get_all_records()
        total = 0
        match_count = 0
        for record in all_records:
            try:
                rec_date = datetime.strptime(record['日付'], '%Y/%m/%d')
                if period == "this_month" and not (rec_date.year == today.year and rec_date.month == today.month): continue
                if keyword != "なし" and (keyword not in str(record.get('項目','')) and keyword not in str(record.get('カテゴリ',''))): continue
                # 文字列クリーニング
                val = str(record.get('金額', 0)).replace('円', '').replace(',', '').strip()
                total += int(val)
                match_count += 1
            except: continue
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"📊 合計は {total:,}円 だよ！({match_count}件)"))
        return

    else:
        prompt = f"「項目,カテゴリ,金額」の形式で1行だけで抽出して：{user_message}"
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}]
        )
        # 最後の1行だけを取得（見出し対策）
        ai_data = response.choices[0].message.content.strip().split('\n')[-1]
        try:
            item, category, amount_str = ai_data.split(',')
            # 金額から「円」や「,」を消す
            amount = amount_str.replace('円', '').replace(',', '').strip()
            
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
