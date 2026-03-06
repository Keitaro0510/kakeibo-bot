import gspread
from oauth2client.service_account import ServiceAccountCredentials
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError # 追加
from linebot.models import MessageEvent, TextMessage, TextSendMessage, ImageSendMessage # ImageSendMessageを追加
from openai import OpenAI
import datetime # ここはこれだけでOK
from datetime import datetime, timedelta # これも残してOKですが、使い分けに注意
import matplotlib.pyplot as plt
import japanize_matplotlib
import io
import os
import json
from dotenv import load_dotenv

app = Flask(__name__)

# .envファイルから環境変数を読み込む
load_dotenv()

# --- 設定（環境変数から読み込むように変更） ---
line_bot_api = LineBotApi(os.getenv('LINE_CHANNEL_ACCESS_TOKEN'))
handler = WebhookHandler(os.getenv('LINE_CHANNEL_SECRET'))
client = OpenAI(api_key=os.getenv('OPENAI_API_KEY'))
# ------------------------------------------


# Googleスプレッドシートの設定
scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']

# 環境変数（GOOGLE_CREDENTIALS_JSON）から設定を読み込む
env_creds = os.getenv('GOOGLE_CREDENTIALS_JSON')
if env_creds:
    # Render（本番環境）用
    creds_dict = json.loads(env_creds)
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
else:
    # ローカルPCでのテスト用（ファイルがあれば読み込む）
    creds = ServiceAccountCredentials.from_json_keyfile_name('google_key.json', scope)

gs_client = gspread.authorize(creds)
# スプレッドシートの名前を正確に入力してください
sheet = gs_client.open("家計簿くんデータ").sheet1 
# ------------------------------------------
def create_pie_chart(data):
    # カテゴリごとの合計を計算
    category_totals = {}
    for record in data:
        cat = record['カテゴリ']
        try:
            amt = int(record['金額'])
            category_totals[cat] = category_totals.get(cat, 0) + amt
        except:
            continue

    if not category_totals:
        return None

    # グラフの作成
    labels = list(category_totals.keys())
    values = list(category_totals.values())

    plt.figure(figsize=(6, 6))
    plt.pie(values, labels=labels, autopct='%1.1f%%', startangle=140, shadow=True)
    plt.title("今月の支出内訳")

    # 画像をメモリ上に保存（ファイルとして保存せず、データとして扱う）
    buf = io.BytesIO()
    plt.savefig(buf, format='png')
    buf.seek(0)
    plt.close()
    return buf
# ------------------------------------------

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_message = event.message.text
    today = datetime.now() # ここでエラーが出ていた可能性あり

    # --- AIによる意図解析 ---
    intent_prompt = f"""
    以下のメッセージの意図を分析し、結果を必ず「意図,キーワード,期間」の形式で1行で返して。
    
    【意図の種類】
    - record: 支出を記録したい場合
    - total: 合計を知りたい場合
    - graph: 支出のグラフを見たい場合（例：グラフ見せて、内訳教えて）

    【期間の種類】
    - this_month, last_month, this_week, all

    メッセージ: {user_message}
    """

    intent_res = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": intent_prompt}]
    )
    
    intent_data = intent_res.choices[0].message.content.strip().split(',')
    if len(intent_data) < 3: # 念のためのガード
        intent, keyword, period = "record", "なし", "なし"
    else:
        intent, keyword, period = intent_data[0].strip(), intent_data[1].strip(), intent_data[2].strip()

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
            # まだ画像送信URLがないので、テキストで成功確認
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="📊 グラフデータは作成できました！次は画像として送る設定をしましょう。"))
        else:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="今月のデータが見つからないので、グラフが作れませんでした。"))
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

    # --- ステップ3: 「記録(record)」の場合の処理（昨日のAI記録コード） ---


    # AIへの依頼（JSON形式で返してもらうように指示）
    prompt = f"""
    以下のメッセージから「金額」「項目」「カテゴリ」を抽出して。
    カテゴリは（食費、日用品、交際費、交通費、その他）から選んで。
    結果は必ず以下の形式だけで返して。余計な説明は不要。
    項目,カテゴリ,金額
    例: ラーメン,食費,900
    
    メッセージ: {user_message}
    """

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}]
    )
    
    ai_data = response.choices[0].message.content.strip()
    
    try:
        # AIの回答を分割してスプレッドシートに書き込み
        item, category, amount = ai_data.split(',')
        # datetime.now() を使って日付を作る
        today_str = datetime.now().strftime('%Y/%m/%d')

        # スプレッドシートの末尾に追加
        sheet.append_row([today_str, item, category, amount])
        
        reply_text = f"✅ 記録したよ！\n日付: {today}\n項目: {item}\nカテゴリ: {category}\n金額: {amount}円"
    except Exception as e:
        reply_text = f"エラーが出ちゃった：{ai_data}"

    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))

# callback関数などの共通部分は省略（昨日のものをそのまま残してください）
@app.route("/callback", methods=['POST'])
def callback():
    # 昨日のコードをそのままここに置いてください
    from linebot.exceptions import InvalidSignatureError
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'

if __name__ == "__main__":

    app.run(port=5000)



