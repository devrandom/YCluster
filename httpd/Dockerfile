FROM nginx:alpine
RUN apk add --no-cache python3 py3-pip
RUN pip3 install flask
COPY start.sh /start.sh
COPY app.py /app.py
RUN chmod +x /start.sh
CMD ["/start.sh"]
