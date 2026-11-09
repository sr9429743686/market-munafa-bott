name: BSE PDF Analyzer Bot

on:
  schedule:
    - cron: '*/30 * * * *'  # દર 30 મિનિટે ઓટોમેટિક રન થશે
  workflow_dispatch:        # બટન દબાવીને મેન્યુઅલી રન કરવા માટે

permissions:
  contents: write           # ડેટાબેઝ ફાઇલ પાછી ગિટહબમાં સેવ કરવા માટે જરૂરી પરમિશન

jobs:
  run-bot:
    runs-on: ubuntu-latest

    steps:
      - name: Checkout Repository
        uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.11'

      - name: Install Dependencies
        run: |
          python -m pip install --upgrade pip
          pip install requests beautifulsoup4 curl_cffi PyPDF2 google-generativeai fake-useragent

      - name: Run BSE PDF Bot
        env:
          BLOG_ID: ${{ secrets.BLOG_ID }}
          CLIENT_ID: ${{ secrets.CLIENT_ID }}
          CLIENT_SECRET: ${{ secrets.CLIENT_SECRET }}
          REFRESH_TOKEN: ${{ secrets.REFRESH_TOKEN }}
          GOOGLE_CLIENT_ID: ${{ secrets.GOOGLE_CLIENT_ID }}
          GOOGLE_CLIENT_SECRET: ${{ secrets.GOOGLE_CLIENT_SECRET }}
          GOOGLE_REFRESH_TOKEN: ${{ secrets.GOOGLE_REFRESH_TOKEN }}
          GEMINI_API_KEY: ${{ secrets.GEMINI_API_KEY }}
        run: |
          python bse_pdf_bot.py

      - name: Save Seen Database (Prevent Duplicates)
        if: always()
        run: |
          git config --global user.name "MarketMunafa Bot"
          git config --global user.email "actions@github.com"
          git add marketmunafa_seen.sqlite3 || true
          git diff --staged --quiet || (git commit -m "Update seen filings database [skip ci]" && git push)
