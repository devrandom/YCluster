#!/bin/sh
python3 app.py &
nginx -g 'daemon off;'
