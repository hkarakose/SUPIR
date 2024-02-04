
pip install --upgrade pip
pip install -r requirements.txt

conda create -n SUPIR python=3.8 -y
conda activate SUPIR
pip install --upgrade pip
pip install -r requirements.txt

# install from source 
# https://github.com/openai/triton?tab=readme-ov-file#install-from-source
git clone https://github.com/openai/triton.git
cd triton
git checkout -t origin/release/2.1.x
pip install ninja cmake wheel # build-time dependencies
pip install -e python

pip install torchvision 

brew install llvm libomp
export CC=/usr/local/opt/llvm/bin/clang

brew install git-lfs
git lfs install
git lfs install --system

mkdir -p "/opt/data/private/AIGC_pretrain/LLaVA1.5"
sudo chown -R halil:staff /opt/data/private
cd /opt/data/private/AIGC_pretrain

git clone https://huggingface.co/openai/clip-vit-large-patch14
git clone "https://huggingface.co/stabilityai/stable-diffusion-xl-base-1.0"

cd clip-vit-large-patch14
wget https://huggingface.co/openai/clip-vit-large-patch14/resolve/main/model.safetensors?download=true


