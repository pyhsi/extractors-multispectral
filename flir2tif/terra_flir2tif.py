#!/usr/bin/env python

import os
import logging
import shutil
import numpy
import tempfile

from pyclowder.utils import CheckMessage
from pyclowder.files import upload_to_dataset
from pyclowder.datasets import download_metadata, upload_metadata
from terrautils.metadata import get_extractor_metadata, get_terraref_metadata, calculate_scan_time
from terrautils.extractors import TerrarefExtractor, is_latest_file, \
    build_dataset_hierarchy, build_metadata, load_json_file
from terrautils.formats import create_geotiff, create_image
from terrautils.spatial import geojson_to_tuples

import Get_FLIR as getFlir


def add_local_arguments(parser):
    # add any additional arguments to parser
    parser.add_argument('--scale', dest="scale_values", type=bool, nargs='?', default=True,
                        help="scale individual flir images based on px range as opposed to full field stitch")

class FlirBin2JpgTiff(TerrarefExtractor):
    def __init__(self):
        super(FlirBin2JpgTiff, self).__init__()

        add_local_arguments(self.parser)

        # parse command line and load default logging configuration
        self.setup(sensor='ir_geotiff')

        # assign other arguments
        self.scale_values = self.args.scale_values

    def check_message(self, connector, host, secret_key, resource, parameters):
        if "rulechecked" in parameters and parameters["rulechecked"]:
            return CheckMessage.download

        if not is_latest_file(resource):
            self.log_skip(resource, "not latest file")
            return CheckMessage.ignore

        # Check for an ir.BIN file before beginning processing
        found_ir = None
        for f in resource['files']:
            if 'filename' in f and f['filename'].endswith('_ir.bin'):
                found_ir = f['filepath']
        if not (found_ir):
            self.log_skip(resource, "IR bin file not found")
            return CheckMessage.ignore

        # Check if outputs already exist
        if not self.overwrite:
            timestamp = resource['dataset_info']['name'].split(" - ")[1]
            png_path = self.sensors.get_sensor_path(timestamp, ext='png')
            tiff_path = self.sensors.get_sensor_path(timestamp)
            out_dir = os.path.dirname(tiff_path)
            if os.path.exists(png_path) and os.path.exists(tiff_path):
                self.log_skip(resource, "outputs found in %s" % out_dir)
                return CheckMessage.ignore

        # Check metadata to verify we have what we need
        md = download_metadata(connector, host, secret_key, resource['id'])
        if get_extractor_metadata(md, self.extractor_info['name']) and not self.overwrite:
            self.log_skip("metadata indicates it was already processed")
            return CheckMessage.ignore
        if get_terraref_metadata(md):
            return CheckMessage.download
        else:
            self.log_skip("no terraref metadata found")
            return CheckMessage.ignore

    def process_message(self, connector, host, secret_key, resource, parameters):
        self.start_message(resource)

        # Get BIN file and metadata
        bin_file, metadata = None, None
        for f in resource['local_paths']:
            # First check metadata attached to dataset in Clowder for item of interest
            if f.endswith('_dataset_metadata.json'):
                all_dsmd = load_json_file(f)
                metadata = get_terraref_metadata(all_dsmd, 'flirIrCamera')
            # Otherwise, check if metadata was uploaded as a .json file
            elif f.endswith('_ir.bin'):
                bin_file = f
        if None in [bin_file, metadata]:
            self.log_error(resource, "could not locate each of ir+metadata in processing")
            raise ValueError("could not locate each of ir+metadata in processing")

        # Determine output directory
        timestamp = resource['dataset_info']['name'].split(" - ")[1]
        png_path = self.sensors.create_sensor_path(timestamp, ext='png')
        tiff_path = self.sensors.create_sensor_path(timestamp)
        uploaded_file_ids = []

        target_dsid = build_dataset_hierarchy(host, secret_key, self.clowder_user, self.clowder_pass, self.clowderspace,
                                              self.sensors.get_display_name(),
                                              timestamp[:4], timestamp[5:7], timestamp[8:10],
                                              leaf_ds_name=self.sensors.get_display_name()+' - '+timestamp)

        # Upload original Lemnatec metadata to new Level_1 dataset
        md = get_terraref_metadata(all_dsmd)
        md['raw_data_source'] = host + ("" if host.endswith("/") else "/") + "datasets/" + resource['id']
        lemna_md = build_metadata(host, self.extractor_info, target_dsid, md, 'dataset')
        self.log_info(resource, "uploading LemnaTec metadata")
        upload_metadata(connector, host, secret_key, target_dsid, lemna_md)

        skipped_png = False
        if not os.path.exists(png_path) or self.overwrite:
            self.log_info(resource, "creating & uploading %s" % png_path)
            # get raw data from bin file
            raw_data = numpy.fromfile(bin_file, numpy.dtype('<u2')).reshape([480, 640]).astype('float')
            raw_data = numpy.rot90(raw_data, 3)
            create_image(raw_data, png_path, self.scale_values)

            # Only upload the newly generated file to Clowder if it isn't already in dataset
            if png_path not in resource["local_paths"]:
                fileid = upload_to_dataset(connector, host, secret_key, target_dsid, png_path)
                uploaded_file_ids.append(host + ("" if host.endswith("/") else "/") + "files/" + fileid)
            self.created += 1
            self.bytes += os.path.getsize(png_path)
        else:
            skipped_png = True

        if not os.path.exists(tiff_path) or self.overwrite:
            self.log_info(resource, "generating temperature matrix")
            gps_bounds = geojson_to_tuples(metadata['spatial_metadata']['flirIrCamera']['bounding_box'])
            if skipped_png:
                raw_data = numpy.fromfile(bin_file, numpy.dtype('<u2')).reshape([480, 640]).astype('float')
                raw_data = numpy.rot90(raw_data, 3)
            tc = getFlir.rawData_to_temperature(raw_data, metadata) # get temperature

            self.log_info(resource, "creating & uploading %s" % tiff_path)
            # Rename temporary tif after creation to avoid long path errors
            out_tmp_tiff = os.path.join(tempfile.gettempdir(), resource['id'].encode('utf8'))
            create_geotiff(tc, gps_bounds, out_tmp_tiff, None, True, self.extractor_info, metadata)
            shutil.move(out_tmp_tiff, tiff_path)
            if tiff_path not in resource["local_paths"]:
                fileid = upload_to_dataset(connector, host, secret_key, target_dsid, tiff_path)
                uploaded_file_ids.append(host + ("" if host.endswith("/") else "/") + "files/" + fileid)
            self.created += 1
            self.bytes += os.path.getsize(tiff_path)

        # Tell Clowder this is completed so subsequent file updates don't daisy-chain
        metadata = build_metadata(host, self.extractor_info, target_dsid, {
            "files_created": uploaded_file_ids}, 'dataset')
        self.log_info(resource, "uploading extractor metadata")
        upload_metadata(connector, host, secret_key, resource['id'], metadata)

        self.end_message(resource)

if __name__ == "__main__":
    extractor = FlirBin2JpgTiff()
    extractor.start()
