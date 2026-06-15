import sys
import unittest
from unittest.mock import patch, MagicMock

# Ensure project files are importable
sys.path.append('.')

from db import get_setting, set_setting, record_channel_stats, get_channel_analytics
from config import requests_get_with_retry
from fetcher import fetch_feed
import bot

class TestNewFeatures(unittest.TestCase):
    
    def setUp(self):
        # Clear or reset owner_id in DB to avoid MagicMock contamination
        from db import get_connection
        with get_connection() as conn:
            conn.execute("DELETE FROM settings WHERE key = 'owner_id'")
            conn.commit()
        set_setting("owner_id", "12345")
        
        # Set up default test setting values
        set_setting("blacklist_words", "presale, scam, pre-sale")
        set_setting("breaking_keywords", "massive, hack, sec, approved")
        
    def test_blacklist_filter(self):
        # Test content filtering with blacklisted words in title
        mock_feed_content = b"""<?xml version="1.0" encoding="utf-8"?>
        <rss version="2.0">
            <channel>
                <title>Test Feed</title>
                <link>http://test.com</link>
                <description>Test Description</description>
                <item>
                    <title>Bitcoin hits new high</title>
                    <link>http://test.com/1</link>
                    <description>Good news description</description>
                </item>
                <item>
                    <title>Buy this amazing presale token today!</title>
                    <link>http://test.com/2</link>
                    <description>Scammy presale details</description>
                </item>
                <item>
                    <title>Security scam warning for investors</title>
                    <link>http://test.com/3</link>
                    <description>Details about a scam</description>
                </item>
            </channel>
        </rss>
        """
        
        with patch('fetcher.requests_get_with_retry') as mock_get:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.content = mock_feed_content
            mock_get.return_value = mock_response
            
            # Fetch feed should filter out the 2nd and 3rd item
            # Let's clean the DB pub check for test links
            from db import get_connection
            with get_connection() as conn:
                conn.execute("DELETE FROM published_posts WHERE url IN ('http://test.com/1', 'http://test.com/2', 'http://test.com/3')")
                conn.commit()
                
            items = fetch_feed("Test Source", "http://test.com/feed")
            
            # Assertions
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["title"], "Bitcoin hits new high")
            self.assertEqual(items[0]["link"], "http://test.com/1")

    def test_breaking_news_detection(self):
        # Test breaking news keyword detection logic
        keywords = ["sec", "hack", "approved"]
        
        title_breaking = "SEC approved spot Ethereum ETF"
        title_regular = "Ethereum gas fees hit multi-year lows"
        
        is_breaking = any(kw in title_breaking.lower() for kw in keywords)
        is_regular_breaking = any(kw in title_regular.lower() for kw in keywords)
        
        self.assertTrue(is_breaking)
        self.assertFalse(is_regular_breaking)

    def test_channel_stats_recording(self):
        # Test analytics recording and retrieval
        channel_id = "test_channel_123"
        record_channel_stats(channel_id, 1500)
        
        # Retrieve stats
        analytics = get_channel_analytics(channel_id)
        self.assertEqual(analytics["current"], 1500)

    def test_jaccard_similarity(self):
        from processor import jaccard_similarity
        
        # Re-ordered title check
        t1 = "SEC Approves Spot Ethereum ETF"
        t2 = "Ethereum Spot ETFs Approved by SEC"
        self.assertGreater(jaccard_similarity(t1, t2), 0.5)
        
        # Different prefixes check
        t3 = "CryptoRank Drop: Ekiden"
        t4 = "Airdrop: Ekiden"
        self.assertEqual(jaccard_similarity(t3, t4), 1.0)
        
        # Non-duplicate check
        t5 = "Bitcoin price hits all time high"
        t6 = "Cardano launches new staking features"
        self.assertLess(jaccard_similarity(t5, t6), 0.1)

    def test_rss_age_filter(self):
        import email.utils
        import time
        
        pub_date_new = email.utils.formatdate(time.time() - 3600)  # 1 hour ago
        pub_date_old = email.utils.formatdate(time.time() - 48 * 3600)  # 48 hours ago
        
        mock_feed_content = f"""<?xml version="1.0" encoding="utf-8"?>
        <rss version="2.0">
            <channel>
                <title>Test Feed</title>
                <link>http://test.com</link>
                <description>Test Description</description>
                <item>
                    <title>New Bitcoin News</title>
                    <link>http://test.com/new</link>
                    <description>New description</description>
                    <pubDate>{pub_date_new}</pubDate>
                </item>
                <item>
                    <title>Old Bitcoin News</title>
                    <link>http://test.com/old</link>
                    <description>Old description</description>
                    <pubDate>{pub_date_old}</pubDate>
                </item>
            </channel>
        </rss>
        """.encode('utf-8')
        
        with patch('fetcher.requests_get_with_retry') as mock_get:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.content = mock_feed_content
            mock_get.return_value = mock_response
            
            # Clean database entries
            from db import get_connection
            with get_connection() as conn:
                conn.execute("DELETE FROM published_posts WHERE url IN ('http://test.com/new', 'http://test.com/old')")
                conn.commit()
                
            items = fetch_feed("Test Source", "http://test.com/feed")
            
            # Assertions
            # The new item should be included, the old one (>24h) should be skipped
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["link"], "http://test.com/new")

    def test_bybit_date_filter(self):
        from datetime import timedelta
        from config import get_berlin_now
        
        berlin_now = get_berlin_now()
        today_str = berlin_now.strftime("%b %d, %Y")
        old_date = berlin_now - timedelta(days=3)
        old_str = old_date.strftime("%b %d, %Y")
        
        mock_bybit_html = f"""
        <html>
            <body>
                <a href="/en/article/new-listing-xyz-blt1/">XYZ Listing lg...{today_str} New Listings</a>
                <a href="/en/article/old-listing-abc-blt2/">ABC Listing lg...{old_str} New Listings</a>
            </body>
        </html>
        """
        
        with patch('fetcher.requests_get_with_retry') as mock_get:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.text = mock_bybit_html
            mock_get.return_value = mock_response
            
            # Clean database entries
            from db import get_connection
            with get_connection() as conn:
                conn.execute("DELETE FROM published_posts WHERE url IN ('https://announcements.bybit.com/en/article/new-listing-xyz-blt1/', 'https://announcements.bybit.com/en/article/old-listing-abc-blt2/')")
                conn.commit()
                
            items = fetch_feed("Bybit", "https://announcements.bybit.com/en-US/")
            
            # Assertions
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["link"], "https://announcements.bybit.com/en/article/new-listing-xyz-blt1/")

    def test_format_proxy(self):
        from bot import format_proxy
        self.assertEqual(format_proxy("1.2.3.4:8080"), "http://1.2.3.4:8080")
        self.assertEqual(format_proxy("http://1.2.3.4:8080"), "http://1.2.3.4:8080")
        self.assertEqual(format_proxy("https://1.2.3.4:8080"), "https://1.2.3.4:8080")
        self.assertEqual(format_proxy("socks5://user:pass@1.2.3.4:8080"), "socks5://user:pass@1.2.3.4:8080")

    @patch('bot.requests.get')
    @patch('bot.bot.edit_message_text')
    def test_check_proxies_job(self, mock_edit_msg, mock_get):
        from bot import check_proxies_job
        
        # Mock successful check for one proxy, failure for another
        mock_resp_success = MagicMock()
        mock_resp_success.status_code = 200
        
        mock_get.side_effect = [mock_resp_success, Exception("Connection timeout")]
        
        proxies_list = ["1.2.3.4:8080", "http://5.6.7.8:8080"]
        check_proxies_job(12345, 67890, proxies_list)
        
        self.assertTrue(mock_edit_msg.called)
        final_call_args = mock_edit_msg.call_args[1]
        self.assertIn("Результати перевірки проксі", final_call_args["text"])
        self.assertIn("1.2.3.4:8080", final_call_args["text"])
        self.assertIn("5.6.7.8:8080", final_call_args["text"])
        self.assertIn("Працює: <b>1</b>", final_call_args["text"])

    @patch('bot.add_admin')
    @patch('bot.delete_admin')
    @patch('bot.bot.send_message')
    def test_admin_step_handlers(self, mock_send_msg, mock_delete_admin, mock_add_admin):
        from bot import process_add_admin_btn, process_delete_admin_btn
        
        # Mock database actions
        mock_add_admin.return_value = True
        mock_delete_admin.return_value = True
        
        # Add admin test
        mock_message_add = MagicMock()
        mock_message_add.text = "987654321 test_user"
        mock_message_add.chat.id = 12345
        mock_message_add.from_user.id = 12345
        
        process_add_admin_btn(mock_message_add)
        mock_add_admin.assert_called_with(987654321, "test_user")
        self.assertTrue(mock_send_msg.called)
        self.assertIn("успішно додано", mock_send_msg.call_args[0][1])
        
        # Delete admin test
        mock_message_delete = MagicMock()
        mock_message_delete.text = "987654321"
        mock_message_delete.chat.id = 12345
        mock_message_delete.from_user.id = 12345
        
        process_delete_admin_btn(mock_message_delete)
        mock_delete_admin.assert_called_with(987654321)
        self.assertIn("успішно видалено", mock_send_msg.call_args[0][1])

    def test_daily_schedule_persistence(self):
        from db import get_connection
        from config import get_berlin_now
        
        today_str = get_berlin_now().date().isoformat()
        
        # 1. Clear today's schedule first
        with get_connection() as conn:
            conn.execute("DELETE FROM daily_schedule WHERE date(post_time) = ?", (today_str,))
            conn.commit()
            
        # 2. Generate new schedule (force=False)
        bot.generate_daily_schedule(force=False)
        
        # Verify entries exist
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT id, post_time, post_type FROM daily_schedule WHERE date(post_time) = ? ORDER BY id ASC", (today_str,))
            rows1 = [dict(row) for row in cursor.fetchall()]
            
        self.assertGreater(len(rows1), 0)
        
        # 3. Call generate_daily_schedule(force=False) again, check they are persistent (not deleted/regenerated)
        bot.generate_daily_schedule(force=False)
        
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT id, post_time, post_type FROM daily_schedule WHERE date(post_time) = ? ORDER BY id ASC", (today_str,))
            rows2 = [dict(row) for row in cursor.fetchall()]
            
        self.assertEqual(rows1, rows2)
        
        # 4. Force regeneration, check that they are modified/different
        bot.generate_daily_schedule(force=True)
        
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT id, post_time, post_type FROM daily_schedule WHERE date(post_time) = ? ORDER BY id ASC", (today_str,))
            rows3 = [dict(row) for row in cursor.fetchall()]
            
        # Since force=True deletes and recreates, the primary key IDs should be different (higher because of AUTOINCREMENT)
        ids1 = [r["id"] for r in rows1]
        ids3 = [r["id"] for r in rows3]
        self.assertNotEqual(ids1, ids3)

    @patch('bot.time.sleep', side_effect=KeyboardInterrupt)
    @patch('bot.run_publish_cycle_by_type', return_value=True)
    def test_scheduler_catchup_spacing(self, mock_publish, mock_sleep):
        from db import get_connection
        from datetime import datetime, timedelta
        from config import get_berlin_now
        
        now = get_berlin_now()
        today_str = now.date().isoformat()
        
        # 1. Clear and insert 3 pending overdue posts
        with get_connection() as conn:
            conn.execute("DELETE FROM daily_schedule WHERE date(post_time) = ?", (today_str,))
            
            # Post 1 (overdue by 2 hours)
            time1 = (now - timedelta(hours=2)).strftime('%Y-%m-%d %H:%M:%S')
            conn.execute("INSERT INTO daily_schedule (post_time, post_type, is_executed) VALUES (?, 'news', 0)", (time1,))
            
            # Post 2 (overdue by 1.5 hours)
            time2 = (now - timedelta(hours=1.5)).strftime('%Y-%m-%d %H:%M:%S')
            conn.execute("INSERT INTO daily_schedule (post_time, post_type, is_executed) VALUES (?, 'activity', 0)", (time2,))
            
            # Post 3 (overdue by 1 hour)
            time3 = (now - timedelta(hours=1)).strftime('%Y-%m-%d %H:%M:%S')
            conn.execute("INSERT INTO daily_schedule (post_time, post_type, is_executed) VALUES (?, 'news', 0)", (time3,))
            
            conn.commit()
            
        # 2. Run scheduler loop once
        try:
            bot.scheduler_thread()
        except KeyboardInterrupt:
            pass
            
        # 3. Verify that the first post was executed (marked is_executed=1)
        # And the remaining two were spaced out (still is_executed=0, but rescheduled by 10 and 20 mins)
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT id, post_time, post_type, is_executed FROM daily_schedule WHERE date(post_time) = ? ORDER BY id ASC", (today_str,))
            rows = [dict(row) for row in cursor.fetchall()]
            
        # We inserted 3 posts, let's verify their status
        self.assertEqual(len(rows), 3)
        
        # First post executed successfully
        self.assertEqual(rows[0]["is_executed"], 1)
        self.assertEqual(rows[0]["post_type"], "news")
        
        # Second post was spaced out (now + 10 mins)
        self.assertEqual(rows[1]["is_executed"], 0)
        self.assertEqual(rows[1]["post_type"], "activity")
        expected_time2 = (now + timedelta(minutes=10)).strftime('%Y-%m-%d %H:%M')
        self.assertTrue(rows[1]["post_time"].startswith(expected_time2))
        
        # Third post was spaced out (now + 20 mins)
        self.assertEqual(rows[2]["is_executed"], 0)
        self.assertEqual(rows[2]["post_type"], "news")
        expected_time3 = (now + timedelta(minutes=20)).strftime('%Y-%m-%d %H:%M')
        self.assertTrue(rows[2]["post_time"].startswith(expected_time3))

    @patch('bot.time.sleep', side_effect=KeyboardInterrupt)
    @patch('bot.run_publish_cycle_by_type', return_value=False)
    @patch('bot.notify_admins_of_failure')
    def test_scheduler_failure_rescheduling(self, mock_notify, mock_publish, mock_sleep):
        from db import get_connection
        from datetime import datetime, timedelta
        from config import get_berlin_now
        
        now = get_berlin_now()
        today_str = now.date().isoformat()
        
        # 1. Clear and insert 1 pending overdue post
        with get_connection() as conn:
            conn.execute("DELETE FROM daily_schedule WHERE date(post_time) = ?", (today_str,))
            # Post overdue by 15 mins
            time1 = (now - timedelta(minutes=15)).strftime('%Y-%m-%d %H:%M:%S')
            conn.execute("INSERT INTO daily_schedule (post_time, post_type, is_executed) VALUES (?, 'news', 0)", (time1,))
            conn.commit()
            
        # 2. Run scheduler loop once
        try:
            bot.scheduler_thread()
        except KeyboardInterrupt:
            pass
            
        # 3. Verify failure behavior:
        # - The post is NOT marked is_executed=1 (remains 0)
        # - The post's post_time is pushed by 30 minutes (now + 30 mins)
        # - Admin notification helper was called
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT id, post_time, post_type, is_executed FROM daily_schedule WHERE date(post_time) = ? ORDER BY id ASC", (today_str,))
            rows = [dict(row) for row in cursor.fetchall()]
            
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["is_executed"], 0) # Remained 0
        expected_reschedule = (now + timedelta(minutes=30)).strftime('%Y-%m-%d %H:%M')
        self.assertTrue(rows[0]["post_time"].startswith(expected_reschedule))
        
        # Admin notification called
        self.assertTrue(mock_notify.called)
        mock_notify.assert_called_with("news")

    @patch('bot.is_admin', return_value=True)
    @patch('bot.handle_analytics')
    @patch('bot.handle_regenerate')
    @patch('bot.handle_backup_db')
    @patch('bot.handle_start')
    def test_new_reply_keyboard_buttons(self, mock_start, mock_backup, mock_regenerate, mock_analytics, mock_is_admin):
        from bot import handle_menu_buttons
        
        # Test 📈 Аналітика
        mock_msg = MagicMock()
        mock_msg.text = "📈 Аналітика"
        mock_msg.from_user.id = 12345
        handle_menu_buttons(mock_msg)
        mock_analytics.assert_called_once_with(mock_msg)
        
        # Test 🔄 Оновити розклад
        mock_msg.text = "🔄 Оновити розклад"
        mock_msg.from_user.id = 12345
        handle_menu_buttons(mock_msg)
        mock_regenerate.assert_called_once_with(mock_msg)
        
        # Test 💾 Резервна копія БД
        mock_msg.text = "💾 Резервна копія БД"
        mock_msg.from_user.id = 12345
        handle_menu_buttons(mock_msg)
        mock_backup.assert_called_once_with(mock_msg)
        
        # Test ℹ️ Довідка
        mock_msg.text = "ℹ️ Довідка"
        mock_msg.from_user.id = 12345
        handle_menu_buttons(mock_msg)
        mock_start.assert_called_once_with(mock_msg)

    @patch('bot.bot.clear_step_handler_by_chat_id')
    @patch('bot.bot.process_new_messages')
    def test_check_cancel_command_with_menu_button(self, mock_process, mock_clear):
        from bot import check_cancel_command
        
        mock_msg = MagicMock()
        mock_msg.text = "📊 Статус"
        mock_msg.chat.id = 12345
        
        res = check_cancel_command(mock_msg)
        self.assertTrue(res)
        mock_clear.assert_called_once_with(chat_id=12345)
        mock_process.assert_called_once_with([mock_msg])
        
        # Test regular text does not cancel
        mock_msg.text = "Just some text"
        res_regular = check_cancel_command(mock_msg)
        self.assertFalse(res_regular)

if __name__ == "__main__":
    unittest.main()
