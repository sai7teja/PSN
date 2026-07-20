#!/usr/bin/env python3
import time
import os
import sys
import gc
import logging
from datetime import datetime
from psnawp_api import PSNAWP
from prometheus_remote_writer import RemoteWriter

# Set up robust logging for Kubernetes (stdout)
logging.basicConfig(
    level=logging.INFO, 
    format='%(asctime)s - %(levelname)s - %(message)s',
    stream=sys.stdout
)
logger = logging.getLogger(__name__)

def get_env_var(name, default=None, required=False):
    """Safely fetch environment variables."""
    value = os.getenv(name, default)
    if required and not value:
        logger.error(f"Missing required environment variable: {name}")
        sys.exit(1)
    return value

def sanitize_label(value, max_length=128):
    """Sanitize strings for Prometheus labels."""
    if not value:
        return 'unknown'
    sanitized = str(value).replace('"', '').replace("'", '').replace('\n', ' ').replace('\r', ' ')
    if len(sanitized) > max_length:
        sanitized = sanitized[:max_length]
    return sanitized

def fetch_stats(token):
    """Connects to PSN, fetches data, and formats for Grafana Cloud."""
    metrics = []
    # Grafana Cloud expects milliseconds for timestamps
    ts = int(time.time() * 1000)

    try:
        psn = PSNAWP(token)
        client = psn.me()
        logger.info("Connected to PSN API successfully.")

        # ==========================================
        # 1. Fetch Account Summary
        # ==========================================
        summary = client.trophy_summary()
        metrics.extend([
            {'metric': {'__name__': 'psn_account_trophy_level'}, 'values': [summary.trophy_level or 0], 'timestamps': [ts]},
            {'metric': {'__name__': 'psn_account_trophy_progress'}, 'values': [summary.progress or 0], 'timestamps': [ts]},
            {'metric': {'__name__': 'psn_account_trophy_tier'}, 'values': [summary.tier or 0], 'timestamps': [ts]}
        ])

        if summary.earned_trophies:
            metrics.append({'metric': {'__name__': 'psn_trophies_total', 'type': 'platinum'}, 'values': [summary.earned_trophies.platinum or 0], 'timestamps': [ts]})
            metrics.append({'metric': {'__name__': 'psn_trophies_total', 'type': 'gold'}, 'values': [summary.earned_trophies.gold or 0], 'timestamps': [ts]})
            metrics.append({'metric': {'__name__': 'psn_trophies_total', 'type': 'silver'}, 'values': [summary.earned_trophies.silver or 0], 'timestamps': [ts]})
            metrics.append({'metric': {'__name__': 'psn_trophies_total', 'type': 'bronze'}, 'values': [summary.earned_trophies.bronze or 0], 'timestamps': [ts]})
            
            total = (summary.earned_trophies.platinum + summary.earned_trophies.gold + 
                     summary.earned_trophies.silver + summary.earned_trophies.bronze)
            metrics.append({'metric': {'__name__': 'psn_trophies_total', 'type': 'total'}, 'values': [total], 'timestamps': [ts]})

        # ==========================================
        # 2. Fetch Trophy Titles (Per Game Stats)
        # ==========================================
        trophy_titles = list(client.trophy_titles())
        metrics.append({'metric': {'__name__': 'psn_games_total'}, 'values': [len(trophy_titles)], 'timestamps': [ts]})

        for title in trophy_titles:
            title_name = sanitize_label(title.title_name)
            metrics.append({'metric': {'__name__': 'psn_title_progress_percent', 'title': title_name}, 'values': [title.progress or 0], 'timestamps': [ts]})
            
            if title.earned_trophies:
                metrics.append({'metric': {'__name__': 'psn_title_earned_trophies', 'title': title_name, 'type': 'platinum'}, 'values': [title.earned_trophies.platinum or 0], 'timestamps': [ts]})
                metrics.append({'metric': {'__name__': 'psn_title_earned_trophies', 'title': title_name, 'type': 'gold'}, 'values': [title.earned_trophies.gold or 0], 'timestamps': [ts]})
                metrics.append({'metric': {'__name__': 'psn_title_earned_trophies', 'title': title_name, 'type': 'silver'}, 'values': [title.earned_trophies.silver or 0], 'timestamps': [ts]})
                metrics.append({'metric': {'__name__': 'psn_title_earned_trophies', 'title': title_name, 'type': 'bronze'}, 'values': [title.earned_trophies.bronze or 0], 'timestamps': [ts]})

        # ==========================================
        # 3. Fetch Play Time Statistics
        # ==========================================
        try:
            titles = list(client.title_stats())
            for title in titles:
                title_name = sanitize_label(title.name)
                
                if title.play_duration:
                    play_minutes = title.play_duration.total_seconds() / 60
                    metrics.append({'metric': {'__name__': 'psn_play_time_minutes', 'title': title_name}, 'values': [play_minutes], 'timestamps': [ts]})
                
                if title.play_count is not None:
                    metrics.append({'metric': {'__name__': 'psn_play_count', 'title': title_name}, 'values': [title.play_count], 'timestamps': [ts]})
        except Exception as e:
            logger.warning(f"Failed to fetch play time stats: {e}")

        # Record successful sync
        metrics.append({'metric': {'__name__': 'psn_data_last_sync_success'}, 'values': [1], 'timestamps': [ts]})
        metrics.append({'metric': {'__name__': 'psn_data_last_sync'}, 'values': [int(time.time())], 'timestamps': [ts]})
        
        logger.info(f"Successfully formatted {len(metrics)} metric data points.")

    except Exception as e:
        logger.error(f"Error fetching PSN data: {e}")
        metrics.append({'metric': {'__name__': 'psn_data_last_sync_success'}, 'values': [0], 'timestamps': [ts]})

    finally:
        # Force garbage collection to keep RAM usage strictly low for the 1GB VM
        gc.collect()

    return metrics

def main():
    logger.info("=" * 60)
    logger.info("PSN Exporter to Grafana Cloud Starting...")
    logger.info("=" * 60)
    
    # Load and validate environment variables
    psn_token = get_env_var('PSN_TOKEN', required=True)
    grafana_url = get_env_var('GRAFANA_URL', required=True)
    grafana_user = get_env_var('GRAFANA_USER', required=True)
    grafana_api_key = get_env_var('GRAFANA_API_KEY', required=True)
    
    # Default to 1 hour (3600s) if not specified to avoid PSN API rate limits
    sync_interval = int(get_env_var('SYNC_INTERVAL', 3600))
    
    logger.info(f"Sync Interval: {sync_interval} seconds")
    logger.info(f"Grafana Endpoint: {grafana_url}")
    logger.info("=" * 60)

    # Initialize the Grafana Cloud remote writer
    writer = RemoteWriter(url=grafana_url, auth=(grafana_user, grafana_api_key))

    # Polling Loop
    while True:
        try:
            logger.info("Initiating PSN data fetch...")
            metrics = fetch_stats(psn_token)
            
            if metrics:
                writer.send(metrics)
                logger.info(f"✅ Successfully pushed {len(metrics)} metrics to Grafana Cloud.")
            else:
                logger.warning("⚠️ No metrics were generated to push.")
                
        except KeyboardInterrupt:
            logger.info("Shutting down exporter safely...")
            break
        except Exception as e:
            logger.error(f"❌ Failed to push metrics to Grafana Cloud: {e}")
        
        logger.info(f"Sleeping for {sync_interval} seconds before next sync...")
        time.sleep(sync_interval)

if __name__ == '__main__':
    main()
