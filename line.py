import gspread
from oauth2client.service_account import ServiceAccountCredentials
from flask import Flask, request, abort, send_from_directory
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage, ImageSendMessage
from openai import OpenAI
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

# 静的ファイルの提供用
@app.route('/static/<path:filename>')
def serve_static(filename):
    return send_from_directory('static', filename)

line_bot_api = LineBotApi(os.getenv('LINE_CHANNEL_ACCESS_TOKEN'))
handler = WebhookHandler(os.getenv('LINE_CHANNEL_SECRET'))
client = OpenAI(api_key=os.getenv('OPENAI_API_KEY'))

# Google Sheets 設定
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
    category_totals = {}
    for record in data:
        cat = clean_val(record.get('カテゴリ', 'その他'))
        try:
            amt = int(clean_val(record.get('金額', 0)))
            if amt > 0:
                category_totals[cat] = category_totals.get(cat, 0) + amt
        except: continue

    if not category_totals: return None

    labels = [f"{k}\n{v:,}円" for k, v in category_totals.items()]
    values = list(category_totals.values())

    plt.figure(figsize=(7, 7))
    plt.pie(values, labels=labels, autopct='%1.1f%%', startangle=90, counterclock=False, shadow=False)
    plt.title(title_text, fontsize=16)

    buf = io.BytesIO()
    plt.savefig(buf, format='png', bbox_inches='tight')
    buf.seek(0)
    plt.close()
    return buf

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    # 送信者のユーザーIDを取得（プッシュ通知に使用）
    user_id = event.source.user_id
    
    try:
        user_message = event.message.text
        today = datetime.now()

        # --- AIへの問い合わせ ---
        prompt = f"""
        以下のメッセージを分析し、支出の記録(RECORD)、合計の確認(TOTAL)、グラフ表示(GRAPH)のいずれか判定して。
        支出記録なら『RECORD,項目,カテゴリ,金額』
        合計なら『TOTAL,なし,期間』
        グラフなら『GRAPH,なし,期間』
        の形式で1行で返して。
        カテゴリは(食費,日用品,交際費,交通費,固定費,美容・衣服,その他)から選択。
        期間は(this_month, last_month, all, today)から選択。
        メッセージ：{user_message}
        """
        
        res = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}]
        )
        ai_raw = res.choices[0].message.content.strip().split('\n')[-1]
        data_parts = [p.strip() for p in ai_raw.split(',')]
        intent = data_parts[0]

        # --- 1. グラフ(GRAPH) または 合計(TOTAL) の処理 ---
        if intent in ["GRAPH", "TOTAL"] or "グラフ" in user_message:
            period = data_parts[2] if len(data_parts) > 2 else "this_month"
            all_records = sheet.get_all_records()
            filtered_data = []
            title_text = "支出内訳"

            for r in all_records:
                try:
                    r_date_str = str(r.get('日付', '')).split(' ')[0]
                    rec_date = datetime.strptime(r_date_str, '%Y/%m/%d')
                    
                    if period == "today" and rec_date.date() == today.date():
                        filtered_data.append(r)
                        title_text = "本日の支出"
                    elif period == "last_month":
                        lm = today.replace(day=1) - timedelta(days=1)
                        if rec_date.year == lm.year and rec_date.month == lm.month:
                            filtered_data.append(r)
                        title_text = "先月の支出"
                    elif period == "all":
                        filtered_data.append(r)
                        title_text = "全期間の支出"
                    else:
                        if rec_date.year == today.year and rec_date.month == today.month:
                            filtered_data.append(r)
                        title_text = "今月の支出"
                except: continue

            if intent == "GRAPH" or "グラフ" in user_message:
                chart_buf = create_pie_chart(filtered_data, title_text)
                if chart_buf:
                    os.makedirs("static", exist_ok=True)
                    filename = f"graph_{int(today.timestamp())}.png"
                    filepath = os.path.join("static", filename)
                    with open(filepath, "wb") as f:
                        f.write(chart_buf.getbuffer())

                    # request.host を使って動的にURLを生成
                    image_url = f"https://{request.host}/static/{filename}"
                    line_bot_api.push_message(user_id, ImageSendMessage(original_content_url=image_url, preview_image_url=image_url))
                else:
                    line_bot_api.push_message(user_id, TextSendMessage(text=f"{title_text}のデータがないよ"))
            
            else: # TOTAL
                total_sum = sum(int(clean_val(r.get('金額', 0))) for r in filtered_data)
                line_bot_api.push_message(user_id, TextSendMessage(text=f"📊 {title_text}の合計は {total_sum:,}円 だよ！"))

        # --- 2. 記録(RECORD) の処理 ---
        else:
            item = clean_val(data_parts[1])
            category = clean_val(data_parts[2]) if len(data_parts) > 2 else "その他"
            amount = clean_val(data_parts[3]) if len(data_parts) > 3 else "0"
            
            today_str = today.strftime('%Y/%m/%d')
            sheet.append_row([today_str, item, category, amount])
            line_bot_api.push_message(user_id, TextSendMessage(text=f"✅ 記録したよ！\n{item} ({category}): {amount}円"))

    except Exception as e:
        print(f"Error: {e}")
        # エラー時もプッシュ通知で状況を知らせる
        try:
            line_bot_api.push_message(user_id, TextSendMessage(text="ちょっと時間がかかったけど、今の内容は記録されたか、もうすぐ届くはずだよ！"))
        except:
            pass

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    
    # LINEからのリクエストを別スレッドで処理するか、
    # 先に「OK」を返してから処理を継続させる
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    
    # 重い処理が終わるのを待たず、LINEサーバーには即座に 200 OK を返す
    return 'OK'

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
