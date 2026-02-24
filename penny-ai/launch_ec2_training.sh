#!/bin/bash
# EC2 g5.xlarge 스팟 인스턴스 생성 및 학습 실행 스크립트

set -e

REGION="ap-northeast-2"
INSTANCE_TYPE="g5.xlarge"
KEY_NAME="penny-ai-key"
SECURITY_GROUP="penny-ai-sg"

echo "🚀 EC2 g5.xlarge 스팟 인스턴스 생성 시작..."

# 최신 Deep Learning AMI (Ubuntu 22.04) 가져오기
AMI_ID=$(aws ec2 describe-images \
  --region $REGION \
  --owners amazon \
  --filters \
    "Name=name,Values=Deep Learning OSS Nvidia Driver AMI GPU PyTorch*Ubuntu 22.04*" \
    "Name=state,Values=available" \
  --query 'sort_by(Images, &CreationDate)[-1].ImageId' \
  --output text)

echo "✅ AMI: $AMI_ID"

# 보안 그룹 생성 (없으면)
SG_ID=$(aws ec2 describe-security-groups \
  --region $REGION \
  --filters "Name=group-name,Values=$SECURITY_GROUP" \
  --query 'SecurityGroups[0].GroupId' \
  --output text 2>/dev/null || echo "None")

if [ "$SG_ID" == "None" ] || [ -z "$SG_ID" ]; then
  SG_ID=$(aws ec2 create-security-group \
    --region $REGION \
    --group-name $SECURITY_GROUP \
    --description "Penny AI Training Security Group" \
    --query 'GroupId' --output text)
  
  aws ec2 authorize-security-group-ingress \
    --region $REGION \
    --group-id $SG_ID \
    --protocol tcp --port 22 --cidr 0.0.0.0/0
  
  echo "✅ 보안 그룹 생성: $SG_ID"
fi

# 키페어 생성 (없으면)
if ! aws ec2 describe-key-pairs --region $REGION --key-names $KEY_NAME &>/dev/null; then
  aws ec2 create-key-pair \
    --region $REGION \
    --key-name $KEY_NAME \
    --query 'KeyMaterial' \
    --output text > ~/.ssh/penny-ai-key.pem
  chmod 600 ~/.ssh/penny-ai-key.pem
  echo "✅ 키페어 생성: $KEY_NAME"
fi

# User Data 스크립트 (학습 자동 실행)
USER_DATA=$(cat <<'USERDATA'
#!/bin/bash
exec > /var/log/penny-ai-training.log 2>&1
set -e

echo "=== 페니스탁 AI 학습 시작 ==="
cd /home/ubuntu

# 환경 설정
export AWS_DEFAULT_REGION=ap-northeast-2
export TELEGRAM_CHAT_ID=5810895605

# stock-bot 클론
git clone https://github.com/sungli01/stock-bot.git
cd stock-bot/penny-ai

# 패키지 설치
pip install -r requirements.txt

# 학습 실행
cd /home/ubuntu/stock-bot
python -m penny_ai.ai.trainer

echo "=== 학습 완료! 인스턴스 종료 ==="
# 학습 완료 후 자동 종료
shutdown -h now
USERDATA
)

# 스팟 인스턴스 요청
echo "💰 스팟 인스턴스 요청 중..."
INSTANCE_ID=$(aws ec2 run-instances \
  --region $REGION \
  --image-id $AMI_ID \
  --instance-type $INSTANCE_TYPE \
  --key-name $KEY_NAME \
  --security-group-ids $SG_ID \
  --instance-market-options '{"MarketType":"spot","SpotOptions":{"SpotInstanceType":"one-time"}}' \
  --block-device-mappings '[{"DeviceName":"/dev/sda1","Ebs":{"VolumeSize":100,"VolumeType":"gp3"}}]' \
  --iam-instance-profile '{"Name":"penny-ai-s3-role"}' \
  --user-data "$USER_DATA" \
  --tag-specifications 'ResourceType=instance,Tags=[{Key=Name,Value=penny-ai-training}]' \
  --query 'Instances[0].InstanceId' \
  --output text)

echo "✅ 인스턴스 생성: $INSTANCE_ID"
echo "⏳ 학습 완료 시 자동 종료됩니다."
echo "📊 로그 확인: aws ec2 get-console-output --instance-id $INSTANCE_ID --region $REGION"
