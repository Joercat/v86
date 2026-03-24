FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y \
    qemu-system-x86 \
    qemu-utils \
    python3 \
    python3-pip \
    socat \
    procps \
    wget \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
RUN mkdir -p /app/uploads /app/disks /app/templates

COPY requirements.txt /app/
RUN pip3 install --no-cache-dir -r requirements.txt

COPY app.py /app/
COPY vnc_client.py /app/
COPY templates/ /app/templates/

RUN qemu-img create -f qcow2 /app/disks/disk_small.qcow2 2G && \
    qemu-img create -f qcow2 /app/disks/disk_medium.qcow2 8G && \
    qemu-img create -f qcow2 /app/disks/disk_large.qcow2 20G

RUN wget -q -O /app/uploads/puppy.iso https://jshshdgxh-storage.static.hf.space || true

RUN chmod -R 777 /app/uploads /app/disks

RUN useradd -m -u 1000 user
USER user
ENV HOME=/home/user PATH=/home/user/.local/bin:$PATH

EXPOSE 7860 

CMD ["python3", "app.py"]
