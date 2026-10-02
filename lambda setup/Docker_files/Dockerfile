FROM mcr.microsoft.com/playwright/python:v1.63.0-noble
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY lambda_function.py .
ENV HOME=/tmp
ENTRYPOINT ["python", "-m", "awslambdaric"]
CMD ["lambda_function.lambda_handler"]