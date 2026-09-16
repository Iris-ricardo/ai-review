FROM node:22-alpine AS frontend-build
WORKDIR /build/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.13-slim

# apt 包（均为运行硬需求）：libreoffice-writer=DOCX→PDF；fonts-noto-cjk=LibreOffice 中文渲染；
# fonts-wqy-microhei=报告 PDF 字体（reportlab 只支持 TrueType，Noto CJK 是 CFF/OTTO 会报
# "postscript outlines are not supported"）；tesseract-ocr* =扫描件 OCR 中英文语言包。
RUN apt-get update && apt-get install -y --no-install-recommends \
    libreoffice-writer \
    fonts-noto-cjk \
    fonts-wqy-microhei \
    tesseract-ocr \
    tesseract-ocr-chi-sim \
    tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/*

# 把镜像内的 TrueType 中文字体固化为 compose 默认 REPORT_FONT_PATH 的稳定路径：
# 按名查找（找不到即中断构建）、0644/0755 保证 appuser 可读；reportlab 按内容识别字体，不依赖扩展名。
RUN set -eux; \
    src=""; \
    for name in wqy-microhei.ttc wqy-zenhei.ttc uming.ttc ukai.ttc DroidSansFallbackFull.ttf DroidSansFallback.ttf; do \
        found="$(find -L /usr/share/fonts -type f -name "$name" | sort | head -n 1)"; \
        if [ -n "$found" ]; then src="$found"; break; fi; \
    done; \
    if [ -z "$src" ]; then \
        src="$(find -L /usr/share/fonts -type f \( -name '*wqy*.ttc' -o -name '*uming*.ttc' -o -name '*ukai*.ttc' -o -name '*Fallback*.ttf' \) | sort | head -n 1)"; \
    fi; \
    if [ -z "$src" ]; then \
        echo "ERROR: no TrueType CJK font found in the image; check the fonts-wqy-microhei install." >&2; \
        exit 1; \
    fi; \
    mkdir -p /usr/local/share/fonts/review; \
    cp -f "$src" /usr/local/share/fonts/review/review-cjk.ttc; \
    chmod 0755 /usr/local/share/fonts/review; \
    chmod 0644 /usr/local/share/fonts/review/review-cjk.ttc; \
    test -r /usr/local/share/fonts/review/review-cjk.ttc; \
    if command -v fc-cache >/dev/null 2>&1; then fc-cache -f; fi

WORKDIR /app
COPY backend/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ ./backend/
COPY rules/ ./rules/
COPY --from=frontend-build /build/frontend/dist ./frontend/dist/

# 非 root 运行用户；/data 是 compose 的 review-data 卷挂载点，
# 卷首次创建时会继承这里的属主与权限，因此 appuser 对上传/输出目录可写。
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /data/uploads /data/outputs \
    && chown -R appuser:appuser /data /app/backend \
    && chmod 0755 /data /data/uploads /data/outputs
USER appuser
WORKDIR /app/backend

# 构建期以 appuser 身份真实注册字体：同时证明文件存在、非 root 可读、轮廓被 reportlab 支持。
RUN python -c "from reportlab.pdfbase import pdfmetrics; from reportlab.pdfbase.ttfonts import TTFont; pdfmetrics.registerFont(TTFont('ReviewCJK', '/usr/local/share/fonts/review/review-cjk.ttc', subfontIndex=0)); print('report font registration OK')"

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
