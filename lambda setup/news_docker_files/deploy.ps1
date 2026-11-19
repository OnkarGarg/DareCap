# Build the news image, push it to ECR, and point the barchart-news function at it.
# Run from anywhere:  & ".\lambda setup\news_files\deploy.ps1"
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$REGION   = "eu-north-1"
$FUNCTION = "barchart-news"
$ACCT     = aws sts get-caller-identity --query Account --output text
if ($LASTEXITCODE -ne 0) { throw "aws sts failed (not logged in?)" }
$REGISTRY = "$ACCT.dkr.ecr.$REGION.amazonaws.com"
$REPO     = "$REGISTRY/barchart_news"

aws ecr get-login-password --region $REGION | docker login --username AWS --password-stdin $REGISTRY
if ($LASTEXITCODE -ne 0) { throw "docker login failed (is Docker Desktop running?)" }

docker build --platform linux/amd64 --provenance=false -t "${REPO}:latest" .
if ($LASTEXITCODE -ne 0) { throw "docker build failed" }

docker push "${REPO}:latest"
if ($LASTEXITCODE -ne 0) { throw "docker push failed" }

aws lambda update-function-code --function-name $FUNCTION --image-uri "${REPO}:latest" --region $REGION --query "CodeSha256" --output text
if ($LASTEXITCODE -ne 0) { throw "update-function-code failed" }

aws lambda wait function-updated --function-name $FUNCTION --region $REGION
Write-Host "Deployed ${REPO}:latest to $FUNCTION"