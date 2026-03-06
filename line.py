import gspread
from oauth2client.service_account import ServiceAccountCredentials
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.models import MessageEvent, TextMessage, TextSendMessage
from openai import OpenAI
import datetime
from datetime import datetime, timedelta

app = Flask(__name__)

import os
from dotenv import load_dotenv

# .envファイルから環境変数を読み込む
load_dotenv()

# --- 設定（環境変数から読み込むように変更） ---
line_bot_api = LineBotApi(os.getenv('LINE_CHANNEL_ACCESS_TOKEN'))
handler = WebhookHandler(os.getenv('LINE_CHANNEL_SECRET'))
client = OpenAI(api_key=os.getenv('OPENAI_API_KEY'))
# ------------------------------------------

import json

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

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_message = event.message.text
    today = datetime.now()

    # --- ステップ1: AIに「ユーザーの意図」を解析させる ---
    intent_prompt = f"""
    以下のメッセージの意図を分析し、結果を必ず「意図,キーワード,期間」の形式で1行で返して。
    
    【意図の種類】
    - record: 支出を記録したい場合（例：ラーメン 900円）
    - total: 合計を知りたい場合（例：今月の食費は？、コンビニでいくら使った？）

    【期間の種類】
    - this_month, last_month, this_week, all

    【出力例】
    - 記録なら: record,なし,なし
    - 集計なら: total,コンビニ,this_month
    - 集計なら: total,食費,last_month

    メッセージ: {user_message}
    """

    intent_res = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": intent_prompt}]
    )
    intent_data = intent_res.choices[0].message.content.strip().split(',')
    intent, keyword, period = intent_data[0], intent_data[1], intent_data[2]

    # --- ステップ2: 「集計(total)」の場合の処理 ---
    if intent == "total":
        all_records = sheet.get_all_records()
        total = 0
        match_count = 0
        
        for record in all_records:
            try:
                rec_date = datetime.strptime(record['日付'], '%Y/%m/%d')
                # 期間フィルタ
                if period == "this_month" and not (rec_date.year == today.year and rec_date.month == today.month): continue
                if period == "last_month":
                    lm = today.replace(day=1) - timedelta(days=1)
                    if not (rec_date.year == lm.year and rec_date.month == lm.month): continue
                
                # キーワードフィルタ（項目名かカテゴリに含まれているか）
                if keyword != "なし":
                    if keyword not in record['項目'] and keyword not in record['カテゴリ']:
                        continue

                total += int(record['金額'])
                match_count += 1
            except: continue

        res_period = {"this_month":"今月", "last_month":"先月", "this_week":"今週", "all":"全期間", "なし":""}[period]
        res_key = f"【{keyword}】" if keyword != "なし" else "全部"
        
        if match_count > 0:
            reply_text = f"📊 {res_period}の{res_key}合計は {total:,}円 だよ！({match_count}件)"
        else:
            reply_text = f"🔍 {res_period}の{res_key}に関するデータは見つからなかったよ。"
        
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
        today = datetime.date.today().strftime('%Y/%m/%d')
        
        # スプレッドシートの末尾に追加
        sheet.append_row([today, item, category, amount])
        
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