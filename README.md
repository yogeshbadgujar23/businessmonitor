# Business Update Monitor

A simple, automated tool to fetch daily updates from your favorite business websites and prepare them for AI summarization.

## Setup

1.  **Install Dependencies**:
    ```bash
    pip install -r requirements.txt
    ```

2.  **Configure Sites**:
    Edit `sites.txt` and add the URLs you want to monitor (one per line).

## Usage

Run the monitor:
```bash
python3 monitor.py
```

This will:
1.  Fetch the latest content from all sites in `sites.txt`.
2.  Save the raw text to `data/<date>/`.
3.  Generate a `daily_digest_prompt.txt` that you can paste into ChatGPT/Claude for a summary.
