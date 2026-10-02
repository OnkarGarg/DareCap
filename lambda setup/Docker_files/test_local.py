import os

# lambda_function reads these at import time, so set them first
os.environ["STATE_BUCKET"] = "your-bucket-name"
os.environ["DRIVE_FOLDER_ID"] = "unused"
os.environ["ARCHIVE_DRIVE_FOLDER_ID"] = "unused"
os.environ["AWS_DEFAULT_REGION"] = "eu-north-1"

from lambda_function import zscore

with open("watchlist.csv", "rb") as f:
    df = zscore(f.read())

print(df[["Symbol", "yahoo", "Latest", "mean20", "std20", "zscore"]])