import sys
import datetime
import pandas as pd
import re
import requests
import xml.etree.ElementTree as et
from io import StringIO
import json

api_url = 'https://opendata.fmi.fi/wfs'
resample = '30min'
rain_factor = 2  # this is 1/resample in hours, e.g. 30min->2, 10min->6, 60min->1


def get_fmidata_multipointcoverage(parameters):
    r = requests.get(f'{api_url}?{parameters}')
    # XML root and namespaces
    root = et.fromstring(r.text)
    namespaces = dict([node for _, node in et.iterparse(StringIO(r.text), events=['start-ns'])])
    # Extract name list
    names = list(map(lambda f: f.attrib['name'], root.findall('.//swe:field', namespaces)))
    # Extract Unix timestamps
    timestamps = re.split(r'\s+', root.find('.//gmlcov:positions', namespaces).text)[3:-1:3]
    # Convert Unix timestamps to datetimes with Helsinki timezone
    datetimeindex = pd.to_datetime(sorted(timestamps * len(names)), unit='s')
    datetimeindex = datetimeindex.tz_localize(tz='UTC').tz_convert('Europe/Helsinki')
    # Extract data
    values = re.split(r'\s+', root.find('.//gml:doubleOrNilReasonTupleList', namespaces).text)[1:-1]
    # Get URL for and print property explanations
    property_url = root.find('.//om:observedProperty', namespaces).attrib[
        '{http://www.w3.org/1999/xlink}href']
    print(f'Properties: {property_url}')
    # Create and return DataFrame
    df = pd.DataFrame({
        'name': names * len(timestamps),
        'value': values},
        index=datetimeindex)
    df['value'] = pd.to_numeric(df['value'])  # arvo floatiksi
    df.index.name = 'time'  # Nimetään indeksi
    return df


# Get geoids from https://www.geonames.org
# geoid = 660972  # Turku, Artukainen
latlon = '60.19,24.95'
# List of stored queries https://ilmatieteenlaitos.fi/tallennetut-kyselyt
query = 'fmi::forecast::hirlam::surface::point::multipointcoverage'
df = get_fmidata_multipointcoverage(f'request=getFeature&storedquery_id={query}&latlon={latlon}&timestep=10')
dfp = df.pivot_table(index='time', columns='name', values='value')
rain = dfp[['Precipitation1h', 'TotalCloudCover']]
# rain.head(50)
rain.resample('30min').sum().head(20)
rain16 = rain.resample('30min').sum().head(16)
cloud16 = rain.resample('30min').mean().head(16)
# print(rain16)
colors = []
readable_colors = []
white = [100, 250, 200]

for ind in rain16.index:
    rain, cloud = round(rain16['Precipitation1h'][ind], 1), int(cloud16['TotalCloudCover'][ind])
    if rain > 0:
        if rain >= 1.0:
            curr = [250, 100, 0]
        elif rain >= 0.2:
            curr = [200, 200, 0]
        else:
            curr = white
        #rain_g = int(rain * rain_factor * 25) + 25
        #if rain_g > 255:
        #    rain_g = 255
        #curr = [0, 0, rain_g]
        #curr = [0, 0, rain_g]
    elif cloud < 80:
        # g = b = int(255 * (100 - cloud) / 100)
        g = b = 200
        r = int(100 - cloud) * 2
        curr = [r, g, b]
    else:
        curr = white
    colors += curr
    readable_colors.append([ind.isoformat()] + curr)
    print(curr, ind, rain, cloud)


arr = bytearray(colors)
with open(sys.argv[1], 'wb') as f:
    f.write(arr)

with open(sys.argv[1] + '.json', 'wt') as f:
    f.write(json.dumps(readable_colors, indent=2))
