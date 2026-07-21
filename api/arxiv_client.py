import logging
import re
import time
import urllib
import urllib.request as libreq
import xml.etree.ElementTree as ET
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from api.models import Paper

logger = logging.getLogger(__name__)


ATOM_NS = "{http://www.w3.org/2005/Atom}"


class ArxivClient:
    def __init__(
        self,
        search_query="cat:cs.AI",
        sort_by="lastUpdatedDate",
        sort_order="descending",
        urlopen: Callable = libreq.urlopen,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.search_query = search_query
        self.sort_by = sort_by
        self.sort_order = sort_order
        self.urlopen = urlopen
        self.sleep = sleep

    def _process_paper_entry(self, entry) -> Paper:
        """
        Parses the data retrieved by the ArXiV API call, extracts information, and populates a dict

        Args:
            entry: an XML string

        Returns:
            dict: containing the parsed values
        """
        title = entry.findtext(f"{ATOM_NS}title", default="").strip()
        summary = entry.findtext(f"{ATOM_NS}summary", default="").strip()
        url = entry.findtext(f"{ATOM_NS}id", default="").strip()
        authors = [
            name.text.strip()
            for author in entry.findall(f"{ATOM_NS}author")
            if (name := author.find(f"{ATOM_NS}name")) is not None and name.text
        ]
        return Paper(title=title, authors=authors, summary=summary, url=url)

    def retrieve_daily_results(
        self,
        max_results: int = 50,
        max_retries: int = 5,
        result_limit: int = 1200,
        now: datetime | None = None,
    ) -> list[Paper]:
        """
        Retrieve papers updated within the previous 24 hours.
        Results are fetched in descending update order until the first entry at
        or before the cutoff is encountered.

        Args:
            None

        Returns:
            list: list of dicts (title, authors, summary, url) of parsed information about papers.
        """
        papers = []
        desired_timezone = UTC
        now = now or datetime.now(desired_timezone)
        if now.tzinfo is None:
            now = now.replace(tzinfo=desired_timezone)
        one_day_ago = now.astimezone(desired_timezone) - timedelta(days=1)
        logger.info("Retrieving papers updated after %s", one_day_ago)

        start = 0

        while True:
            url = (
                "https://export.arxiv.org/api/query?"
                f"search_query={self.search_query}"
                f"&sortBy={self.sort_by}"
                f"&sortOrder={self.sort_order}"
                f"&start={start}"
                f"&max_results={max_results}"
            )

            retry_count = 0
            while retry_count < max_retries:
                try:
                    with self.urlopen(url) as response:
                        data = response.read()
                        root = ET.fromstring(data)

                        if len(root.findall(f"{ATOM_NS}entry")) == 0:
                            logger.info("ArXiv returned no matching papers; stopping retrieval")
                            return papers

                        break
                except urllib.error.HTTPError as e:
                    retry_count += 1
                    if retry_count == max_retries:
                        logger.error(
                            "Failed to retrieve data after %d attempts: %s",
                            max_retries,
                            e,
                        )
                        return papers
                    logger.warning("HTTP error on attempt %d: %s", retry_count, e)
                    if e.code == 429:
                        wait_time = 60 * (2 ** (retry_count - 1))
                        logger.info(
                            "Rate limited (429). Waiting %ds before retry %d",
                            wait_time,
                            retry_count + 1,
                        )
                        self.sleep(wait_time)
                    else:
                        self.sleep(5)
                    continue
                except (urllib.error.URLError, ET.ParseError) as e:
                    retry_count += 1
                    if retry_count == max_retries:
                        logger.error(
                            "Failed to retrieve data after %d attempts: %s",
                            max_retries,
                            e,
                        )
                        return papers
                    logger.warning("ArXiv request failed on attempt %d: %s", retry_count, e)
                    self.sleep(5)
                    continue

            for entry in root.findall(f"{ATOM_NS}entry"):
                updated_date_str = entry.find(f"{ATOM_NS}updated").text
                updated_date = datetime.strptime(updated_date_str, "%Y-%m-%dT%H:%M:%S%z")
                updated_date = updated_date.astimezone(desired_timezone)

                if updated_date > one_day_ago:
                    papers.append(self._process_paper_entry(entry))
                else:
                    logger.info(
                        "Reached cutoff at %s; latest cutoff is %s",
                        updated_date,
                        one_day_ago,
                    )
                    return papers

            start += max_results
            logger.info("Latest paper date on page: %s", updated_date)
            self.sleep(5)
            if start > result_limit:
                logger.warning("Found more than %d papers; stopping retrieval", result_limit)
                break

        return papers

    def extract_titles(self, content):
        """
        Extracts titles from the content using regex.

        Titles are assumed to follow the pattern:
        - A number followed by a period (`1.`, `2.`, etc.)
        - The title is enclosed in double asterisks (`**`).

        Args:
            content (str): The string content containing the top 5 papers selected by the LLM.

        Returns:
            list: A list of titles extracted from the content.
        """
        # Match lines starting with a number followed by a title in double asterisks
        matches = re.findall(r"\d+\.\s\*\*(.*?)\*\*", content)
        return matches

    def filter_dicts_by_titles(self, dict_list, titles):
        """
        Filters the dictionaries in the input list by matching titles,
        ignoring special characters and whitespace differences.

        Args:
            dict_list (list): A list of dictionaries, each containing a 'title' key.
            titles (list): A list of titles extracted from the content.

        Returns:
            list: Filtered dictionaries that match the titles.
        """

        def normalize(text):
            # Remove special characters and extra whitespace
            return re.sub(r"\s+", " ", text.strip()).replace("\n", "")

        # Normalize extracted titles for comparison
        normalized_titles = [normalize(title) for title in titles]

        # Filter dictionaries whose normalized title matches any in the normalized titles
        return [item for item in dict_list if normalize(item.get("title", "")) in normalized_titles]

    def get_pdf_url(self, arxiv_url):
        """
        Extracts the PDF URL from an arXiv abstract page URL using the arXiv API.

        Args:
            arxiv_url (str): The URL of the arXiv abstract page.

        Returns:
            str: The URL of the PDF, or None if not found.
        """

        # 1. Extract the arXiv ID from the URL
        match = re.search(r"abs/([\w\.\/]+)", arxiv_url)
        if not match:
            return None
        arxiv_id = match.group(1)

        # 2. Construct the API query URL
        api_url = f"https://export.arxiv.org/api/query?id_list={arxiv_id}"

        try:
            # 3. Call the API and get the Atom feed
            with self.urlopen(api_url) as response:
                xml_content = response.read()

            # 4. Parse the Atom feed
            tree = ET.fromstring(xml_content)

            # Register the namespaces
            namespaces = {
                "atom": "http://www.w3.org/2005/Atom",
                "arxiv": "http://arxiv.org/schemas/atom",
            }

            # 5. Find the PDF link
            for entry in tree.findall("atom:entry", namespaces):
                for link in entry.findall("atom:link", namespaces):
                    if link.get("rel") == "related" and link.get("title") == "pdf":
                        return link.get("href")
            return None

        except urllib.error.URLError as e:
            logger.error(f"Error: Could not retrieve data from the API. {e}")
            return None
        except ET.ParseError as e:
            logger.error(f"Error: Could not parse the XML data. {e}")
            return None
        except Exception as e:
            logger.error(f"An unexpected error occurred {e}")
            return None
