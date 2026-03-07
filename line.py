import gspread
from oauth2client.service_account import ServiceAccountCredentials
from flask import Flask, request, abort, send_from_directory
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError, LineBotApiError
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

@app.route('/static/<path:filename>')
def serve_static(filename):
    return send_from_directory('static', filename)

# 環境変数の読み込みチェック
channel_access_token = os.getenv('LINE_CHANNEL_ACCESS_TOKEN')
channel_secret = os.getenv('LINE_CHANNEL_SECRET')

if not channel_access_token or not channel_secret:
    print("CRITICAL ERROR: LINEの環境変数が設定されていません！")

line_bot_api = LineBotApi(channel_access_token)
handler = WebhookHandler(channel_secret)
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
    # データの集計先を動的に変える（絞り込みがある場合は「項目」、ない場合は「カテゴリ」）
    # フィルタリングされたデータが特定のカテゴリのみなら、項目名で集計した方が見やすい
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

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_id = event.source.user_id
    user_message = event.message.text
    print(f"DEBUG: メッセージを受信しました: {user_message} (UserID: {user_id})")
    
    try:
        today = datetime.now()

        # AI判定（特定のカテゴリ抽出を追加）
        prompt = f"""
        以下のメッセージを判定して。
        支出記録なら『RECORD,項目,カテゴリ,金額』
        合計・グラフなら『TOTALかGRAPH,抽出カテゴリ(なければ"なし"),期間(this_month, last_month, all, today)』
        の形式で1行で返して。
        メッセージ：{user_message}
        """
        res = client.chat.completions.create(model="gpt-4o-mini", messages=[{"role": "user", "content": prompt}])
        ai_raw = res.choices[0].message.content.strip().split('\n')[-1]
        data_parts = [p.strip() for p in ai_raw.split(',')]
        intent = data_parts[0]

        if intent in ["GRAPH", "TOTAL"] or "グラフ" in user_message:
            # 抽出したい特定のカテゴリがあるか確認
            target_category = data_parts[1] if len(data_parts) > 1 else "なし"
            period = data_parts[2] if len(data_parts) > 2 else "this_month"
            
            all_records = sheet.get_all_records()
            filtered_data = []
            title_text = "支出内訳"

            # 期間でフィルタリング
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
                    else: # this_month
                        if rec_date.year == today.year and rec_date.month == today.month: is_in_period = True
                    
                    if is_in_period:
                        # 特定のカテゴリ指定がある場合、さらに絞り込む
                        rec_cat = clean_val(r.get('カテゴリ', ''))
                        if target_category == "なし" or target_category in rec_cat or rec_cat in target_category:
                            filtered_data.append(r)
                except: continue

            # メッセージのタイトルを調整
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
            # --- 記録(RECORD)の処理 ---
            item = clean_val(data_parts[1])
            category = clean_val(data_parts[2]) if len(data_parts) > 2 else "その他"
            amount_str = clean_val(data_parts[3]) if len(data_parts) > 3 else "0"
            amount = int(amount_str)
            
            # シートに追記
            sheet.append_row([today.strftime('%Y/%m/%d'), item, category, amount])

            # --- 予算チェック機能を追加 ---
            try:
                # G1セルから予算を取得 (例: 50000)
                budget = int(sheet.acell('G1').value.replace(',', ''))
                
                # 今月の全データを取得して合計を計算
                all_records = sheet.get_all_records()
                this_month_total = 0
                for r in all_records:
                    try:
                        r_date = datetime.strptime(str(r.get('日付', '')).split(' ')[0], '%Y/%m/%d')
                        if r_date.year == today.year and r_date.month == today.month:
                            this_month_total += int(clean_val(r.get('金額', 0)))
                    except: continue
                
                remaining = budget - this_month_total
                
                # 返信メッセージの作成
                msg = f"✅ 記録したよ！\n{item} ({category}): {amount:,}円\n\n"
                msg += f"📊 今月の合計: {this_month_total:,}円\n"
                
                if remaining > 0:
                    msg += f"💰 今月の残予算: あと {remaining:,}円 だよ。頑張ろう！"
                elif remaining == 0:
                    msg += "⚠️ 予算ピッタリ使い切ったよ！"
                else:
                    msg += f"🚨 予算オーバー！ {abs(remaining):,}円 使いすぎてるよ！"
                
                line_bot_api.push_message(user_id, TextSendMessage(text=msg))

            except Exception as e:
                print(f"BUDGET ERROR: {e}")
                # 予算取得に失敗しても記録完了だけは伝える
                line_bot_api.push_message(user_id, TextSendMessage(text=f"✅ 記録したよ！\n{item}: {amount}円\n(予算取得に失敗しました)"))

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers.get('X-Line-Signature')
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        print("Invalid Signature. Check your channel secret.")
        abort(400)
    return 'OK'

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)


