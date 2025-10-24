FROM joyzoursky/python-chromedriver:3.9
WORKDIR /app
COPY . /app
RUN pip install -r requirements.txt
CMD ["python", "ozon_telegram_bot.py"]
