#!/usr/bin/env python3
"""
PSN Complete Gaming Analytics Exporter v3
==========================================
Pushes ALL available PSNAWP data to Grafana Cloud via Prometheus Remote Write.
Designed for the "PSN Complete Gaming Analytics" dashboard (grafana-dashboard-v2.json).

Metrics pushed:
  Account:    psn_account_trophy_level, psn_account_trophy_progress, psn_account_trophy_tier,
              psn_account_trophies_earned, psn_friends_count, psn_online_status
  Games:      psn_games_total, psn_game_trophy_progress_percent, psn_game_play_time_minutes,
              psn_game_play_count, psn_game_first_played_timestamp, psn_game_last_played_timestamp,
              psn_game_trophy_last_updated_timestamp, psn_game_trophies_earned, psn_game_trophies_defined
  Trophies:   psn_trophy_earned_info, psn_trophy_earned_timestamp
  Health:     psn_data_last_sync_success, psn_data_last_sync_timestamp, psn_sync_duration_seconds,
              psn_token_expired, psn_sync_errors_total
"""

import time
import os
import sys
import gc
import socket
import logging
from datetime import datetime
from psnawp_api import PSNAWP
from prometheus_remote_writer import RemoteWriter

# Prevent hanging API calls on the 1 OCPU VM
socket.setdefaulttimeout(30)

# ==========================================================================
# RATE LIMITING CONFIGURATION
# PSNAWP enforces 300 requests per 15 minutes. We add strategic sleep points
# between API calls to stay well within this budget. The per-game trophy
# fetch loop is the heaviest consumer (~1 call per game with earned trophies).
# ==========================================================================
API_SLEEP_LIGHT = 0.5   # Sleep between lightweight API calls (friends, summary)
API_SLEEP_MEDIUM = 1.0  # Sleep between moderate calls (title_stats pagination)
API_SLEEP_HEAVY = 3.0   # Sleep between heavy calls (per-game trophy detail fetch)

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


def datetime_to_timestamp(dt):
    """Convert datetime to Unix timestamp in seconds."""
    if dt is None:
        return 0
    try:
        return int(dt.timestamp())
    except Exception:
        return 0


def datetime_to_ms(dt):
    """Convert datetime to Unix timestamp in milliseconds (for Grafana display)."""
    if dt is None:
        return 0
    try:
        return int(dt.timestamp() * 1000)
    except Exception:
        return 0


def fetch_stats(token):
    """Connects to PSN, fetches ALL data, and formats for Grafana Cloud remote write."""
    metrics = []
    sync_errors = {}
    api_calls = 0  # Track total PSN API hits per sync cycle
    start_time = time.time()
    # Grafana Cloud remote write expects milliseconds for timestamps
    ts = int(time.time() * 1000)

    try:
        psn = PSNAWP(token)
        client = psn.me()
        api_calls += 1  # PSNAWP auth + me() endpoint
        logger.info("Connected to PSN API successfully.")

        # ==================================================================
        # 1. ACCOUNT SUMMARY
        # ==================================================================
        summary = client.trophy_summary()
        api_calls += 1
        time.sleep(API_SLEEP_LIGHT)  # Rate limit: pause after account summary
        metrics.extend([
            {'metric': {'__name__': 'psn_account_trophy_level'}, 'values': [summary.trophy_level or 0], 'timestamps': [ts]},
            {'metric': {'__name__': 'psn_account_trophy_progress'}, 'values': [summary.progress or 0], 'timestamps': [ts]},
            {'metric': {'__name__': 'psn_account_trophy_tier'}, 'values': [summary.tier or 0], 'timestamps': [ts]},
        ])

        if summary.earned_trophies:
            for trophy_type in ['platinum', 'gold', 'silver', 'bronze']:
                count = getattr(summary.earned_trophies, trophy_type, 0) or 0
                metrics.append({'metric': {'__name__': 'psn_account_trophies_earned', 'type': trophy_type}, 'values': [count], 'timestamps': [ts]})
                # Also push the old metric name for backward compatibility with v1 dashboard
                metrics.append({'metric': {'__name__': 'psn_trophies_total', 'type': trophy_type}, 'values': [count], 'timestamps': [ts]})

            total = (summary.earned_trophies.platinum + summary.earned_trophies.gold +
                     summary.earned_trophies.silver + summary.earned_trophies.bronze)
            metrics.append({'metric': {'__name__': 'psn_account_trophies_earned', 'type': 'total'}, 'values': [total], 'timestamps': [ts]})
            metrics.append({'metric': {'__name__': 'psn_trophies_total', 'type': 'total'}, 'values': [total], 'timestamps': [ts]})

        # ==================================================================
        # 2. SOCIAL METRICS (Friends Count)
        # ==================================================================
        try:
            friends = list(client.friends_list())
            api_calls += 1
            time.sleep(API_SLEEP_LIGHT)  # Rate limit: pause after friends list
            metrics.append({'metric': {'__name__': 'psn_friends_count'}, 'values': [len(friends)], 'timestamps': [ts]})
            logger.info(f"Friends count: {len(friends)}")

            # Push online status of friends (how many are currently online)
            online_count = 0
            try:
                # Try multiple possible method names for presence lookup
                friend_account_ids = [f.account_id for f in friends]
                if friend_account_ids:
                    # PSNAWP library has inconsistent method names across versions
                    get_pres = getattr(client, 'get_presences', None) or getattr(client, 'presence', None)
                    if get_pres:
                        presences = get_pres(friend_account_ids)
                        api_calls += 1
                        time.sleep(API_SLEEP_LIGHT)  # Rate limit: pause after presence check
                        for pres in presences:
                            try:
                                if pres.basic_presence.primary_platform_info.online_status == 'online':
                                    online_count += 1
                            except (AttributeError, TypeError):
                                pass
                    else:
                        logger.info("Presence API not available in this PSNAWP version, skipping.")
            except Exception as e:
                logger.warning(f"Could not fetch friend presences: {e}")

            metrics.append({'metric': {'__name__': 'psn_friends_online_count'}, 'values': [online_count], 'timestamps': [ts]})
        except Exception as e:
            logger.warning(f"Failed to fetch friends list: {e}")
            sync_errors['friends'] = sync_errors.get('friends', 0) + 1

        # ==================================================================
        # 3. TROPHY TITLES (Per-Game Stats with Platform Labels)
        # ==================================================================
        logger.info("Fetching trophy titles...")
        trophy_titles = list(client.trophy_titles())
        api_calls += 1
        time.sleep(API_SLEEP_MEDIUM)  # Rate limit: pause after fetching full game library
        metrics.append({'metric': {'__name__': 'psn_games_total'}, 'values': [len(trophy_titles)], 'timestamps': [ts]})
        logger.info(f"Found {len(trophy_titles)} trophy titles.")

        earned_trophy_count = 0

        for idx, trophy_title in enumerate(trophy_titles, 1):
            if idx % 10 == 0:
                logger.info(f"Processing trophy title {idx}/{len(trophy_titles)}...")

            title_name = sanitize_label(trophy_title.title_name)
            title_id = sanitize_label(trophy_title.np_communication_id)
            platform = sanitize_label(
                str(list(trophy_title.title_platform)[0]) if trophy_title.title_platform else 'unknown'
            )

            # Game trophy progress (used by "Trophy Progress (Top 15)" bar gauge)
            metrics.append({
                'metric': {'__name__': 'psn_game_trophy_progress_percent', 'title': title_name, 'title_id': title_id, 'platform': platform},
                'values': [trophy_title.progress or 0], 'timestamps': [ts]
            })
            # Backward compat with v1 dashboard
            metrics.append({
                'metric': {'__name__': 'psn_title_progress_percent', 'title': title_name},
                'values': [trophy_title.progress or 0], 'timestamps': [ts]
            })

            # Per-game earned trophies
            if trophy_title.earned_trophies:
                for t_type in ['platinum', 'gold', 'silver', 'bronze']:
                    count = getattr(trophy_title.earned_trophies, t_type, 0) or 0
                    metrics.append({
                        'metric': {'__name__': 'psn_game_trophies_earned', 'title': title_name, 'title_id': title_id, 'platform': platform, 'type': t_type},
                        'values': [count], 'timestamps': [ts]
                    })
                    # Backward compat
                    metrics.append({
                        'metric': {'__name__': 'psn_title_earned_trophies', 'title': title_name, 'type': t_type},
                        'values': [count], 'timestamps': [ts]
                    })

            # Per-game defined trophies (how many trophies exist for the game)
            if trophy_title.defined_trophies:
                for t_type in ['platinum', 'gold', 'silver', 'bronze']:
                    count = getattr(trophy_title.defined_trophies, t_type, 0) or 0
                    metrics.append({
                        'metric': {'__name__': 'psn_game_trophies_defined', 'title': title_name, 'title_id': title_id, 'platform': platform, 'type': t_type},
                        'values': [count], 'timestamps': [ts]
                    })

            # Trophy last updated timestamp (attribute name varies by PSNAWP version)
            last_updated = getattr(trophy_title, 'last_updated_datetime', None) or getattr(trophy_title, 'last_updated_date_time', None)
            if last_updated:
                metrics.append({
                    'metric': {'__name__': 'psn_game_trophy_last_updated_timestamp', 'title': title_name, 'title_id': title_id, 'platform': platform},
                    'values': [datetime_to_ms(last_updated)], 'timestamps': [ts]
                })

            # ==============================================================
            # 4. INDIVIDUAL TROPHY DETAILS (earned trophies only)
            # ==============================================================
            total_earned_in_title = 0
            if trophy_title.earned_trophies:
                total_earned_in_title = (
                    (trophy_title.earned_trophies.platinum or 0) +
                    (trophy_title.earned_trophies.gold or 0) +
                    (trophy_title.earned_trophies.silver or 0) +
                    (trophy_title.earned_trophies.bronze or 0)
                )

            if total_earned_in_title > 0 and trophy_title.np_communication_id:
                try:
                    # Rate limit: heavy sleep before each per-game trophy API call
                    time.sleep(API_SLEEP_HEAVY)
                    platform_code = list(trophy_title.title_platform)[0] if trophy_title.title_platform else 'PS5'
                    trophies = list(client.trophies(trophy_title.np_communication_id, platform_code))
                    api_calls += 1

                    for trophy in trophies:
                        if getattr(trophy, 'earned', False):
                            trophy_name = sanitize_label(trophy.trophy_name)
                            trophy_type = str(trophy.trophy_type.value) if trophy.trophy_type else 'unknown'
                            trophy_rarity = str(trophy.trophy_rarity) if hasattr(trophy, 'trophy_rarity') else '0'
                            earned_datetime = trophy.earned_date_time if hasattr(trophy, 'earned_date_time') else None

                            earned_trophy_count += 1

                            # Trophy info marker (value=1 means earned)
                            metrics.append({
                                'metric': {
                                    '__name__': 'psn_trophy_earned_info',
                                    'title': title_name, 'title_id': title_id,
                                    'trophy_name': trophy_name, 'type': trophy_type, 'rarity': trophy_rarity
                                },
                                'values': [1], 'timestamps': [ts]
                            })

                            # Trophy earned timestamp (for the "Trophy Earned History" table)
                            if earned_datetime:
                                metrics.append({
                                    'metric': {
                                        '__name__': 'psn_trophy_earned_timestamp',
                                        'title': title_name, 'title_id': title_id,
                                        'trophy_name': trophy_name, 'type': trophy_type
                                    },
                                    'values': [datetime_to_ms(earned_datetime)], 'timestamps': [ts]
                                })

                except Exception as e:
                    logger.warning(f"Failed to fetch trophies for {title_name}: {e}")
                    sync_errors['trophy_fetch'] = sync_errors.get('trophy_fetch', 0) + 1

        logger.info(f"Processed {earned_trophy_count} individual earned trophies across all games. API calls so far: {api_calls}")

        # ==================================================================
        # 5. PLAY TIME STATISTICS (with first/last played timestamps)
        # ==================================================================
        logger.info("Fetching title play statistics...")
        time.sleep(API_SLEEP_MEDIUM)  # Rate limit: pause before title_stats
        try:
            titles = list(client.title_stats())
            api_calls += 1
            for title in titles:
                title_name = sanitize_label(title.name)
                title_id = sanitize_label(title.title_id) if hasattr(title, 'title_id') else 'unknown'
                platform = sanitize_label(str(title.category)) if hasattr(title, 'category') else 'unknown'

                # Play Time (minutes)
                if title.play_duration:
                    play_minutes = title.play_duration.total_seconds() / 60
                    metrics.append({
                        'metric': {'__name__': 'psn_game_play_time_minutes', 'title': title_name, 'title_id': title_id, 'platform': platform},
                        'values': [play_minutes], 'timestamps': [ts]
                    })
                    # Backward compat with v1
                    metrics.append({
                        'metric': {'__name__': 'psn_play_time_minutes', 'title': title_name},
                        'values': [play_minutes], 'timestamps': [ts]
                    })

                # Play Count
                if title.play_count is not None:
                    metrics.append({
                        'metric': {'__name__': 'psn_game_play_count', 'title': title_name, 'title_id': title_id, 'platform': platform},
                        'values': [title.play_count], 'timestamps': [ts]
                    })
                    # Backward compat
                    metrics.append({
                        'metric': {'__name__': 'psn_play_count', 'title': title_name},
                        'values': [title.play_count], 'timestamps': [ts]
                    })

                # First Played Timestamp (milliseconds for Grafana dateTimeAsSystem unit)
                if hasattr(title, 'first_played_date_time') and title.first_played_date_time:
                    metrics.append({
                        'metric': {'__name__': 'psn_game_first_played_timestamp', 'title': title_name, 'title_id': title_id, 'platform': platform},
                        'values': [datetime_to_ms(title.first_played_date_time)], 'timestamps': [ts]
                    })

                # Last Played Timestamp
                if hasattr(title, 'last_played_date_time') and title.last_played_date_time:
                    metrics.append({
                        'metric': {'__name__': 'psn_game_last_played_timestamp', 'title': title_name, 'title_id': title_id, 'platform': platform},
                        'values': [datetime_to_ms(title.last_played_date_time)], 'timestamps': [ts]
                    })

        except Exception as e:
            logger.warning(f"Failed to fetch title stats: {e}")
            sync_errors['title_stats'] = sync_errors.get('title_stats', 0) + 1

        # ==================================================================
        # 6. SYNC HEALTH METRICS
        # ==================================================================
        end_time = time.time()
        sync_duration = end_time - start_time

        metrics.extend([
            {'metric': {'__name__': 'psn_data_last_sync_success'}, 'values': [1], 'timestamps': [ts]},
            {'metric': {'__name__': 'psn_token_expired'}, 'values': [0], 'timestamps': [ts]},
            {'metric': {'__name__': 'psn_data_last_sync_timestamp'}, 'values': [int(end_time)], 'timestamps': [ts]},
            # Backward compat
            {'metric': {'__name__': 'psn_data_last_sync'}, 'values': [int(end_time)], 'timestamps': [ts]},
            {'metric': {'__name__': 'psn_sync_duration_seconds'}, 'values': [round(sync_duration, 2)], 'timestamps': [ts]},
            # API usage tracking (PSNAWP limit: 300 requests / 15 minutes)
            {'metric': {'__name__': 'psn_api_calls_per_sync'}, 'values': [api_calls], 'timestamps': [ts]},
        ])

        # Push individual error counters
        for error_type, count in sync_errors.items():
            metrics.append({
                'metric': {'__name__': 'psn_sync_errors_total', 'error_type': error_type},
                'values': [count], 'timestamps': [ts]
            })

        logger.info(
            f"✅ Successfully formatted {len(metrics)} metric data points. "
            f"Level={summary.trophy_level}, Games={len(trophy_titles)}, "
            f"Earned Trophies={earned_trophy_count}, API Calls={api_calls}/300, Duration={sync_duration:.2f}s"
        )

    except Exception as e:
        logger.error(f"Error fetching PSN data: {e}")
        metrics.append({'metric': {'__name__': 'psn_data_last_sync_success'}, 'values': [0], 'timestamps': [ts]})

        # If the error is related to authentication, flag the token as expired
        if "Authentication" in str(type(e).__name__) or "401" in str(e) or "403" in str(e) or "npsso" in str(e).lower():
            logger.error("🚨 PSN Token has EXPIRED! Pushing metric to trigger Grafana Alert.")
            metrics.append({'metric': {'__name__': 'psn_token_expired'}, 'values': [1], 'timestamps': [ts]})
        else:
            metrics.append({'metric': {'__name__': 'psn_token_expired'}, 'values': [0], 'timestamps': [ts]})

        metrics.append({'metric': {'__name__': 'psn_sync_errors_total', 'error_type': 'general'}, 'values': [1], 'timestamps': [ts]})

    finally:
        # Force garbage collection to keep RAM usage strictly low for the 1GB VM
        gc.collect()

    return metrics


def main():
    logger.info("=" * 60)
    logger.info("PSN Complete Gaming Analytics Exporter v3 Starting...")
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
    writer = RemoteWriter(url=grafana_url, auth={"username": grafana_user, "password": grafana_api_key})

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
