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

def clean_val(text):
    text = str(text)
    if ":" in text: text = text.split(":")[-1]
    if "：" in text: text = text.split("：")[-1]
    return text.replace('円', '').replace(',', '').strip()

# --- グラフ作成（金額表示付き） ---
def create_pie_chart(data, title_text):
    category_totals = {}
    for record in data:
        cat = clean_val(record.get('カテゴリ', 'その他'))
        try:
            amt = int(clean_val(record.get('金額', 0)))
            if amt > 0:
                category_totals[cat] = category_totals.get(cat, 0) + amt
        except: continue

    if not category_totals: return None

    # ラベルに金額を含める（例：食費\n5,000円）
    labels = [f"{k}\n{v:,}円" for k, v in category_totals.items()]
    values = list(category_totals.values())

    plt.figure(figsize=(7, 7))
    plt.pie(values, labels=labels, autopct='%1.1f%%', startangle=140, counterclock=False)
    plt.title(title_text, fontsize=15)

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

        # 1. 意図解析（期間の抽出を強化）
        intent_prompt = f"""
        以下のメッセージの意図を分析し「意図,キーワード,期間」で返して。
        期間は (this_month, last_month, all, today) のいずれかに分類して。
        メッセージ：{user_message}
        """
        intent_res = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": intent_prompt}]
        )
        intent_raw = intent_res.choices[0].message.content.strip().split('\n')[-1]
        intent_data = intent_raw.split(',')
        intent = intent_data[0].strip()
        period = intent_data[2].strip() if len(intent_data) > 2 else "this_month"

        if "graph" in intent or "グラフ" in user_message or "内訳" in user_message:
            intent = "graph"

        # --- 2. グラフ表示（期間絞り込み対応） ---
        if intent == "graph":
            all_records = sheet.get_all_records()
            filtered_data = []
            title_text = "支出内訳"

            for r in all_records:
                try:
                    rec_date = datetime.strptime(str(r['日付']), '%Y/%m/%d')
                    if period == "today" and rec_date.date() == today.date():
                        filtered_data.append(r)
                        title_text = "本日の支出"
                    elif period == "last_month":
                        last_month = today.replace(day=1) - timedelta(days=1)
                        if rec_date.year == last_month.year and rec_date.month == last_month.month:
                            filtered_data.append(r)
                        title_text = "先月の支出"
                    elif period == "all":
                        filtered_data.append(r)
                        title_text = "全期間の支出"
                    else: # デフォルトは今月
                        if rec_date.year == today.year and rec_date.month == today.month:
                            filtered_data.append(r)
                        title_text = "今月の支出"
                except: continue

            chart_buf = create_pie_chart(filtered_data, title_text)
            if chart_buf:
                os.makedirs("static", exist_ok=True)
                filepath = os.path.join("static", "graph.png")
                with open(filepath, "wb") as f:
                    f.write(chart_buf.getbuffer())

                image_url = f"https://{request.host}/static/graph.png?{int(today.timestamp())}"
                line_bot_api.reply_message(event.reply_token, ImageSendMessage(original_content_url=image_url, preview_image_url=image_url))
            else:
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"{title_text}のデータがないよ"))
            return

        # --- 3. 支出記録（カテゴリー推論を強化） ---
        else:
            record_prompt = f"""
            以下のメッセージから「項目,カテゴリ,金額」を抽出して。
            カテゴリは(食費, 日用品, 交際費, 交通費, 固定費, 美容・衣服, その他)から最適なものを選んで。
            メッセージ：{user_message}
            返信形式：項目,カテゴリ,金額（例：コーヒー,食費,450）
            """
            ai_res = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": record_prompt}]
            )
            ai_data = ai_res.choices[0].message.content.strip().split('\n')[-1]
            parts = ai_data.split(',')
            item = clean_val(parts[0])
            category = clean_val(parts[1]) if len(parts) > 1 else "その他"
            amount = clean_val(parts[2]) if len(parts) > 2 else "0"
            
            sheet.append_row([today.strftime('%Y/%m/%d'), item, category, amount])
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"✅ 記録したよ！\n{item} ({category}): {amount}円"))

    except Exception as e:
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"エラー：{str(e)}"))

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
