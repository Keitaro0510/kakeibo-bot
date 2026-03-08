import gspread
from oauth2client.service_account import ServiceAccountCredentials
from flask import Flask, request, abort, send_from_directory
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError, LineBotApiError
from linebot.models import MessageEvent, TextMessage, TextSendMessage, ImageSendMessage, ImageMessage
from openai import OpenAI
from datetime import datetime, timedelta
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import japanize_matplotlib
import io
import os
import json
import base64
from dotenv import load_dotenv

app = Flask(__name__)
load_dotenv()

@app.route('/static/<path:filename>')
def serve_static(filename):
    return send_from_directory('static', filename)

channel_access_token = os.getenv('LINE_CHANNEL_ACCESS_TOKEN')
channel_secret = os.getenv('LINE_CHANNEL_SECRET')

line_bot_api = LineBotApi(channel_access_token)
handler = WebhookHandler(channel_secret)
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
    text = str(text).replace('項目:', '').replace('カテゴリ:', '').replace('金額:', '')
    if ":" in text: text = text.split(":")[-1]
    if "：" in text: text = text.split("：")[-1]
    return text.replace('円', '').replace(',', '').strip()

def create_pie_chart(data, title_text):
    target_key = '項目' if '【' in title_text else 'カテゴリ'
    label_totals = {}
    for record in data:
        key = clean_val(record.get(target_key, 'その他'))
        try:
            amt = int(clean_val(record.get('金額', 0)))
            if amt > 0:
                label_totals[key] = label_totals.get(key, 0) + amt
        except: continue
    if not label_totals: return None
    labels = [f"{k}\n{v:,}円" for k, v in label_totals.items()]
    values = list(label_totals.values())
    plt.figure(figsize=(7, 7))
    plt.pie(values, labels=labels, autopct='%1.1f%%', startangle=90, counterclock=False)
    plt.title(title_text, fontsize=16)
    buf = io.BytesIO()
    plt.savefig(buf, format='png', bbox_inches='tight')
    buf.seek(0)
    plt.close()
    return buf

# 予算と合計を計算してメッセージを作る共通関数
def get_budget_message(today, item, category, amount):
    try:
        budget = int(sheet.acell('G1').value.replace(',', ''))
        all_records = sheet.get_all_records()
        this_month_total = 0
        for r in all_records:
            try:
                r_date = datetime.strptime(str(r.get('日付', '')).split(' ')[0], '%Y/%m/%d')
                if r_date.year == today.year and r_date.month == today.month:
                    this_month_total += int(clean_val(r.get('金額', 0)))
            except: continue
        
        remaining = budget - this_month_total
        msg = f"✅ 記録したよ！\n{item} ({category}): {amount:,}円\n\n📊 今月の合計: {this_month_total:,}円\n"
        if remaining > 0: msg += f"💰 残予算: あと {remaining:,}円"
        elif remaining == 0: msg += "⚠️ 予算ピッタリ使い切った！"
        else: msg += f"🚨 予算オーバー！ {abs(remaining):,}円 使いすぎ"
        return msg
    except:
        return f"✅ 記録完了！\n{item}: {amount:,}円"

# --- 画像メッセージ（レシート）の処理 ---
@handler.add(MessageEvent, message=ImageMessage)
def handle_image(event):
    user_id = event.source.user_id
    print("DEBUG: 画像を受信しました。レシート解析を開始します。")
    
    try:
        # 1. LINEから画像データを取得
        message_content = line_bot_api.get_message_content(event.message.id)
        image_bytes = io.BytesIO(message_content.content).read()
        base64_image = base64.b64encode(image_bytes).decode('utf-8')

        # 2. OpenAI Vision APIで解析
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "このレシートから「店名(項目)」「カテゴリ」「合計金額」を抽出して。形式：項目,カテゴリ,金額 (例: セブンイレブン,食費,540)。カテゴリは食費、日用品、交際費、交通費、趣味、衣服、美容、医療、住居、水道光熱、その他から選んで。1行で回答して。"},
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}}
                    ],
                }
            ],
            max_tokens=300,
        )

        # 3. 解析結果を処理
        ai_res = response.choices[0].message.content.strip()
        print(f"DEBUG: レシート解析結果: {ai_res}")
        parts = [p.strip() for p in ai_res.split(',')]
        
        if len(parts) >= 3:
            item = parts[0]
            category = parts[1]
            amount = int(clean_val(parts[2]))
            
            today = datetime.now()
            sheet.append_row([today.strftime('%Y/%m/%d'), item, category, amount])
            
            # 予算計算を含むメッセージを送信
            final_msg = get_budget_message(today, item, category, amount)
            line_bot_api.push_message(user_id, TextSendMessage(text="📸 レシートを読み取ったよ！\n" + final_msg))
        else:
            line_bot_api.push_message(user_id, TextSendMessage(text="ごめん、レシートがうまく読めなかったよ。もう一度明るい場所で撮ってみて。"))

    except Exception as e:
        print(f"IMAGE ERROR: {e}")
        line_bot_api.push_message(user_id, TextSendMessage(text="画像解析中にエラーが発生しました。"))

# --- テキストメッセージの処理 ---
@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_id = event.source.user_id
    user_message = event.message.text
    print(f"DEBUG: メッセージ受信: {user_message}")
    
    try:
        today = datetime.now()
        prompt = f"""
        あなたは優秀な家計簿オーガナイザーです。
        メッセージから「支出項目」「カテゴリ」「金額」を抽出して。
        
        【カテゴリの例】
        食費、日用品、交際費、交通費、趣味、衣服、美容、医療、住居、水道光熱、その他
        
        【判定ルール】
        - ジュース、コンビニ、スーパー、外食は「食費」
        - 洗剤、ティッシュ、ゴミ袋は「日用品」
        - 電車、タクシー、バスは「交通費」
        - 飲み会、プレゼント、デートは「交際費」
        - 判断に迷うものは「その他」
        
        支出記録なら『RECORD,項目,カテゴリ,金額』
        合計・グラフなら『TOTALかGRAPH,抽出カテゴリ(なければ"なし"),期間(this_month, last_month, all, today)』
        形式厳守。
        メッセージ：{user_message}
        """
        res = client.chat.completions.create(model="gpt-4o-mini", messages=[{"role": "user", "content": prompt}])
        ai_raw = res.choices[0].message.content.strip().split('\n')[-1]
        data_parts = [p.strip() for p in ai_raw.split(',')]
        intent = data_parts[0]

        if intent in ["GRAPH", "TOTAL"] or "グラフ" in user_message:
            target_category = data_parts[1] if len(data_parts) > 1 else "なし"
            period = data_parts[2] if len(data_parts) > 2 else "this_month"
            all_records = sheet.get_all_records()
            filtered_data = []
            for r in all_records:
                try:
                    r_date_str = str(r.get('日付', '')).split(' ')[0]
                    rec_date = datetime.strptime(r_date_str, '%Y/%m/%d')
                    is_in_period = False
                    if period == "today" and rec_date.date() == today.date(): is_in_period = True
                    elif period == "last_month":
                        lm = today.replace(day=1) - timedelta(days=1)
                        if rec_date.year == lm.year and rec_date.month == lm.month: is_in_period = True
                    elif period == "all": is_in_period = True
                    else:
                        if rec_date.year == today.year and rec_date.month == today.month: is_in_period = True
                    
                    if is_in_period:
                        rec_cat = clean_val(r.get('カテゴリ', ''))
                        if target_category == "なし" or target_category in rec_cat or rec_cat in target_category:
                            filtered_data.append(r)
                except: continue

            period_name = {"today":"本日", "last_month":"先月", "all":"全期間", "this_month":"今月"}.get(period, "今月")
            cat_name = f"【{target_category}】" if target_category != "なし" else ""
            title_text = f"{period_name}の{cat_name}支出"

            if intent == "GRAPH" or "グラフ" in user_message:
                chart_buf = create_pie_chart(filtered_data, title_text)
                if chart_buf:
                    os.makedirs("static", exist_ok=True)
                    filename = f"graph_{int(today.timestamp())}.png"
                    filepath = os.path.join("static", filename)
                    with open(filepath, "wb") as f: f.write(chart_buf.getbuffer())
                    image_url = f"https://{request.host}/static/{filename}"
                    line_bot_api.push_message(user_id, ImageSendMessage(original_content_url=image_url, preview_image_url=image_url))
                else:
                    line_bot_api.push_message(user_id, TextSendMessage(text=f"{title_text}のデータがないよ"))
            else:
                total_sum = sum(int(clean_val(r.get('金額', 0))) for r in filtered_data)
                line_bot_api.push_message(user_id, TextSendMessage(text=f"📊 {title_text}の合計は {total_sum:,}円 だよ！"))

        else:
            item = clean_val(data_parts[1])
            category = clean_val(data_parts[2]) if len(data_parts) > 2 else "その他"
            amount = int(clean_val(data_parts[3]) if len(data_parts) > 3 else "0")
            sheet.append_row([today.strftime('%Y/%m/%d'), item, category, amount])

            final_msg = get_budget_message(today, item, category, amount)
            line_bot_api.push_message(user_id, TextSendMessage(text=final_msg))

    except Exception as e:
        print(f"GENERAL ERROR: {e}")

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers.get('X-Line-Signature')
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
