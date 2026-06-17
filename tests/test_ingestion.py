from lbs_predictor.ingestion import clean_csv_value, parse_lbs_response_xml


def test_parse_lbs_response_xml_extracts_coordinates_and_address():
    xml = """
    <root>
      <latitude>23.2599</latitude>
      <longitude>77.4126</longitude>
      <address1>Bhopal</address1>
    </root>
    """

    lat, lon, address = parse_lbs_response_xml(xml)

    assert lat == "23.2599"
    assert lon == "77.4126"
    assert address == "Bhopal"


def test_clean_csv_value_preserves_leading_zeroes():
    assert clean_csv_value('="000123"') == "000123"
