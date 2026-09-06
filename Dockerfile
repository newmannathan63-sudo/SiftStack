# Apify Actor Dockerfile — Python 3.12 + Playwright + Chromium
FROM apify/actor-python-playwright:3.12

# Tesseract OCR — used for scanned PDF/photo notices (tax sale imports) and
# for OCR'ing recorded lis pendens documents (duval_clerk_scraper.py) to pull
# ground-truth property address/parcel ID instead of guessing by owner name.
USER root
RUN apt-get update && apt-get install -y --no-install-recommends tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*
USER myuser

# Copy requirements first for better layer caching
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# patchright ships its own patched Chrome build, separate from the base image's
# bundled Playwright Chromium — used only by jdr_scraper.py to get past
# Cloudflare's CDP-level automation detection on legals.jaxdailyrecord.com.
RUN python -m patchright install chromium

# Copy source code
COPY src/ ./src/
COPY .actor/ ./.actor/

# Playwright browsers are pre-installed in the base image.
# Set working directory so imports from src/ work.
ENV PYTHONPATH=/home/myuser/src

CMD ["python", "src/main.py"]
