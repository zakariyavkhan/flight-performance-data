# from config import DevelopmentConfig, ProductionConfig
from dotenv import dotenv_values
from datetime import datetime, timezone
import requests, logging, sys
from bs4 import BeautifulSoup
from pymongo import MongoClient

config = dotenv_values(".env")
logging.basicConfig(
    filename=config["LOG_FILE"],
    encoding="utf-8",
    level=logging.INFO,
    format="%(asctime)s: %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
LOGGER = logging.getLogger(__name__)


def get_client():
    """
    :return: a connection to the database
    """
    uri = "mongodb://%s" % (config["DB_HOST"],)
    try:
        client = MongoClient(uri)
    except Exception as e:
        LOGGER.error("Error connecting to MongoDB Platform: %s", e)
        exit(1)
    return client


def get_flights(url, table_id):
    """
    Gets the table of flights from the given url
    :param url: the url to scrape
    :param table_id: the id of the table to scrape
    :return: the table of flights
    :rtype: bs4.element.ResultSet
    """
    page = requests.get(url)
    soup = BeautifulSoup(page.content, "html.parser")

    # get the table with id "flightsToday", select rows with class "arrival" or "departure"
    table = soup.find("table", {"id": table_id})
    if table:
        table = table.find_all("tr", class_=["arrival", "departure"])

    return table


def parse_flights(table):
    """
    Parses the table of flights
    :caveat: this function breaks on every Jan 1st for delayed flights
    :param table: the table of flights
    :return: list of flights
    :rtype: [{"flight_num":"WS197",}, {}, ...]
    """
    flights = []
    for row in table:
        flight = {}
        today = datetime.now().strftime("%a %b %d")
        scheduled_time = row.find("div").text.strip()
        # only delayed flight have this div
        actual_time_div = row.find("div", class_="bubble")
        if actual_time_div:
            actual_time = actual_time_div.find_all("div")[1].text.strip()
        else:
            actual_time = None
        flight["gate"] = row.find("td", class_="ft-gate").text.strip()
        flight["airline"] = row.find("span").text.strip()
        flight["src_dest"] = row.find_all("td")[2].text.strip()
        flight["flight_num"] = row.find_all("td")[1].text.strip()

        # timestamps in UTC because that's what MongoDB uses
        flight["scheduled_timestamp"] = (
            (datetime.strptime(today + " " + scheduled_time, "%a %b %d %I:%M %p"))
            .replace(year=datetime.now().year)
            .astimezone(timezone.utc)
        )
        if actual_time:
            # if the flight is delayed past 11:59pm, due to UTC conversion
            # the actual_timestamp will be incorrect. gets fixed on next day
            flight["actual_timestamp"] = (
                (datetime.strptime(today + " " + actual_time, "%a %b %d %I:%M %p"))
                .replace(year=datetime.now().year)
                .astimezone(timezone.utc)
            )

        # one of the advantages of MongoDB is flexibile schema
        # i may want to take advantage of that by only storing keys with not null values
        if "departure" in row["class"]:
            flight["type"] = "departure"
        else:
            flight["type"] = "arrival"

        flights.append(flight)

    return flights


def add_flights(conn, flights):
    """
    Adds the flights to the database
    :param conn: connection to the flights collection
    :param flights: the list of flights to add
    """
    try:
        results = conn.insert_many(flights)
        log_msg = f"Inserted {len(results.inserted_ids)} documents into {config['COLLECTION']}"
    except Exception as e:
        LOGGER.error("Error inserting documents into MongoDB: %s", e)
        exit(1)
    return log_msg


def update_flights(conn, delayed_flights):
    """
    Updates the flights collection with the new flights
    :caveat: if delayed_flights and DB is empty, the wrong flight will be updated
    :param conn: connection to the flights collection
    :param flights: the list of flights to update
    """
    delayed_flights = parse_flights(delayed_flights)
    # for flight in delayed_flights, find the corresponding flight
    # in flights_collection by (scheduled_timestamp, flight_num)
    # and update it's actual_timestamp
    updated = 0
    for flight in delayed_flights:
        query = {
            "scheduled_timestamp": flight["scheduled_timestamp"],
            "flight_num": flight["flight_num"],
        }
        update = {"$set": {"actual_timestamp": flight["actual_timestamp"]}}
        try:
            result = conn.update_one(query, update)
            updated += result.modified_count
        except Exception as e:
            LOGGER.error("Error updating document in MongoDB: %s", e)
            exit(1)
    return f"; Updated {updated} documents"


def save_page():
    """
    Saves the page to a file for testing purposes
    """
    page = requests.get(config["URL"])
    soup = BeautifulSoup(page.content, "html.parser")
    filename = f"pages/{datetime.now().strftime('%Y-%m-%d')}.html"
    with open(filename, "w") as f:
        f.write(str(soup))


def main():
    # # can i configure the output file of the logger after creating it?
    # if "dev" in sys.argv:
    #     config = DevelopmentConfig()
    # else:
    #     config = ProductionConfig()

    save_page()

    delayed_flights = get_flights(config["URL"], "flightsYesterday")
    flight_table = get_flights(config["URL"], "flightsToday")
    flights = parse_flights(flight_table)

    client = get_client()
    db = client[config["DB_NAME"]]
    flights_collection = db[config["COLLECTION"]]

    log_msg = add_flights(flights_collection, flights)
    if delayed_flights:
        log_msg += update_flights(flights_collection, delayed_flights)

    LOGGER.info(log_msg)
    client.close()


if __name__ == "__main__":
    main()
