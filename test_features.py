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

if __name__ == "__main__":
    unittest.main()
