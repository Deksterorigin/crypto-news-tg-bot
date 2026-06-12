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

if __name__ == "__main__":
    unittest.main()
