import logging
import simplejson
import urllib
import urllib2
import urlparse
import requests
import time

from hashlib import md5

from django.conf import settings
from django.template.loader import render_to_string
from django.core.exceptions import ValidationError as DjangoValidationError

from crits.services.core import Service, ServiceConfigError
from crits.pcaps.handlers import handle_pcap_file
from crits.domains.handlers import upsert_domain
from crits.domains.domain import Domain
from crits.core.user_tools import get_user_organization

from . import forms

logger = logging.getLogger(__name__)


class VirusTotalService(Service):
    """
    Check the VirusTotal database to see if it contains this sample, domain
    or IP.

    Requires an API key available from virustotal.com

    TODO:
        - Add IP addresses to domains....maybe.
        - Perform a check to see if the API key is really private
    """

    name = "virustotal_lookup"
    version = '3.2.0'
    supported_types = ['Sample', 'Domain', 'IP']
    required_fields = []
    description = "Look up a Sample, Domain or IP in VirusTotal"

    @staticmethod
    def bind_runtime_form(analyst, config):
        if 'vt_add_pcap' not in config:
            config['vt_add_pcap'] = False
        if 'vt_add_domains' not in config:
            config['vt_add_domains'] = False
        if 'vt_upload_unknown_sample' not in config:
            config['vt_upload_unknown_sample'] = False

        config['vt_wait_for_processing'] = str(config['vt_wait_for_processing'][0])

        return forms.VirusTotalRunForm(config)

    @classmethod
    def generate_runtime_form(self, analyst, config, crits_type, identifier):
        return render_to_string('services_run_form.html',
                                {'name': self.name,
                                'form': forms.VirusTotalRunForm(),
                                'crits_type': crits_type,
                                'identifier': identifier})

    @staticmethod
    def save_runtime_config(config):
        del config['vt_api_key']

    @staticmethod
    def get_config(existing_config):
        # Generate default config from form and initial values.
        config = {}
        fields = forms.VirusTotalConfigForm().fields
        for name, field in fields.iteritems():
            config[name] = field.initial

        # If there is a config in the database, use values from that.
        if existing_config:
            for key, value in existing_config.iteritems():
                config[key] = value
        return config

    @staticmethod
    def parse_config(config):
        if not config['vt_api_key']:
            raise ServiceConfigError("API key required.")

    @classmethod
    def generate_config_form(self, config):
        # Convert sigfiles to newline separated strings
        html = render_to_string('services_config_form.html',
                                {'name': self.name,
                                 'form': forms.VirusTotalConfigForm(initial=config),
                                 'config_error': None})
        form = forms.VirusTotalConfigForm
        return form, html

    @staticmethod
    def get_config_details(config):
        display_config = {}

        # Rename keys so they render nice.
        fields = forms.VirusTotalConfigForm().fields
        for name, field in fields.iteritems():
            display_config[field.label] = config[name]

        return display_config

    def get_pcap(self, md5):
        """
        Retreives a PCAP files from VT using the Private API.

        Args:
            md5 (String): MD5 of the sample we would like to pull the pcap from.

        TODO:
            Add optional switch in the get request to store the pcap as a temp
            file before adding it to CRITs. Switch is stream=True and CRITs
            provides a handler for temp files.
            Make Error message uniform like the rest
        """
        network_url = self.config.get('vt_network_url', '')
        params = {'apikey': self.config.get('vt_api_key', ''), 'hash': md5}

        try:
            response = requests.get(network_url, params=params, proxies=self.proxies)
        except Exception as e:
            logger.error("Virustotal: network connection error for PCAP (%s)" % e)
            self._error("Network connection error checking virustotal for PCAP (%s)" % e)
            return None

        if response.headers['content-type'] == 'application/cap':
            self._info("Gathered PCAP file for %s" % md5)
            return response.content
        elif response.headers['content-type'] == 'application/json':
            self._warning("Could not fetch PCAP for hash %s" % md5)
            return None
        else:
            self._warning("Could not fetch PCAP for unknown reasons")
            return None

    def run(self, obj, config):
        self.config = config
        self.obj = obj

        # Pull configuration and check to see if a key is presented
        private_key = config.get('vt_api_key_private', False)
        pull_pcap = config.get('vt_add_pcap', False)
        key = config.get('vt_api_key', '')
        sample_url = config.get('vt_query_url', '')
        domain_url = config.get('vt_domain_url', '')
        ip_url = config.get('vt_ip_url', '')
        upload_unknown_sample = config.get('vt_upload_unknown_sample', True)
        wait_for_processing = int(config.get('vt_wait_for_processing', 5)) # time in m to wait before asking vt if the processing of a new sample is done
        if not key:
            self._error("No valid VT key found")
            return

        # setup proxy as needed
        self.proxies = {}
        if settings.HTTP_PROXY:
            self.proxies = {"https": settings.HTTP_PROXY} # VT only uses https


        response_dict = self._get_sample_data()
        if not response_dict:
            self._error("Getting sample data from VT failed!")
            return

        # VT response codes:
        # 0 = unknown to vt
        # 1 = known and ready
        # -2 = queued for analysis
        response_code = response_dict.get('response_code', 0)


        if response_code != 1:

            if response_code == -2: # sample is already queued for analysis - just wait a bit!
                self._info("Waiting {} minutes for VT processing".format(wait_for_processing))
                time.sleep(float(60*wait_for_processing))

            elif response_code == 0 and upload_unknown_sample: # sample is unknown AND we want to upload it
                if not self._upload_sample():
                    return # self._error is set by _upload_sample

                self._info("Waiting {} minutes for VT processing".format(wait_for_processing))
                time.sleep(float(60*wait_for_processing))
                response_dict = self._get_sample_data()

            else: # sample is unknown and we DON'T want to upload it
                self._error("Exiting because Virustotal provided a negative response code.")
                return

            response_dict = self._get_sample_data()
            if not response_dict or response_dict.get('response_code', 0) != 1:
                self._error("Getting sample data from VT failed at last!")
                return

        # Process Results for Sample
        if obj._meta['crits_type'] == 'Sample':
            # If we are missing any hashes for this file, add them
            self._process_hashes(response_dict)


            # Process Public Key data and store scandate for later use
            response = self._process_public_sample(response_dict)
            if not response['success']:
                self._info(response['message'])
            scandate = response.get('scandate', None)

            # If selected, process private key data
            if private_key:
                ###
                # TODO: catch to see if the key is really private
                ###

                # Process private metadata
                response = self._process_private_sample_metadata(response_dict)
                if not response['success']:
                    self._info(response['message'])

                # Process private VirusTotal metadata
                response = self._process_private_sample_vtmetadata(response_dict)
                if not response['success']:
                    self._info(response['message'])

                # Process private VirusTotal behaviour information
                response = self._process_private_sample_behaviour(response_dict, scandate)
                if not response['success']:
                    self._info(response['message'])

                # Pull pcap file, add to DB, and create relationship
                if pull_pcap:
                    pcap = self.get_pcap(obj.md5)
                    if pcap:
                        self._process_pcap(pcap, scandate)

        # Process results for Domain
        elif obj._meta['crits_type'] == 'Domain':
            # Process public VirusTotal domain information
            response = self._process_public_domain(response_dict)
            if not response['success']:
                self._info(response['message'])

        # Process results for IP
        elif obj._meta['crits_type'] == 'IP':
            # Process public VirusTotal ip information
            response = self._process_public_ip(response_dict)
            if not response['success']:
                self._info(response['message'])

    def _get_sample_data(self):
        """
        Simple get sample data

        Return: False on failure, dict with json response on success
        """

        key = self.config.get('vt_api_key', '')

        if self.obj._meta['crits_type'] == 'Sample':
            if self.config.get('vt_api_key_private', False):
                params = {"resource": self.obj.md5, "apikey": key, 'allinfo': 1}
            else:
                params = {"resource": self.obj.md5, "apikey": key}
            url = "https://www.virustotal.com/vtapi/v2/file/report"

        elif self.obj._meta['crits_type'] == 'Domain':
            params = {'domain': self.obj.domain, 'apikey': key}
            url = "https://www.virustotal.com/vtapi/v2/domain/report"

        elif self.obj._meta['crits_type'] == 'IP':
            params = {'ip': self.obj.ip, 'apikey': key}
            url = "https://www.virustotal.com/vtapi/v2/ip-address/report"

        else:
            logger.error("Virustotal: Unsupported sample type for %s" % self.obj.md5)
            return False


        try:
            response = requests.get(url, params=params, proxies=self.proxies)
            response_dict = response.json()
        except Exception as e:
            logger.error("Virustotal: network connection error (%s)" % e)
            return False

        return response_dict


    def _upload_sample(self):
        """
        see function name...
        (supports sample / domain)

        Return: Bool
        """

        key = self.config.get('vt_api_key', '')

        if self.obj._meta['crits_type'] == 'Sample':
            ###
            #TODO: If the sample is larger than 32mb we need to go a different route
            ###

            params = {'apikey': key}
            f = {'file': (self.obj.filename, self.obj.filedata.read())}
            response = requests.post('https://www.virustotal.com/vtapi/v2/file/scan', files=f, params=params, proxies=self.proxies)

        elif self.obj._meta['crits_type'] == 'Domain':
            params = {'apikey': key, 'url':self.obj.domain}
            response = requests.post('https://www.virustotal.com/vtapi/v2/url/scan', params=params, proxies=self.proxies)

        else:
            self._error("Exiting because Virustotal did not know the sample AND we currently cant upload this type of sample!")
            return False


        upload_response = response.json()

        if upload_response.get('response_code', 0) != 1:
            self._error("Exiting because Virustotal did not know the sample AND we couldn't upload it. Msg.: " + upload_response['verbose_msg'])
            return False

        return True


    def _process_hashes(self, report):
        """
        Process hash data from VirusTotal.

        Args:
            report (dict): json report information

        Return: None
        """

        save = False

#        if self.obj.ssdeep != report['ssdeep']:
#            self.obj.ssdeep = report['ssdeep']
#            save = True
        if self.obj.sha1 != report['sha1']:
            self.obj.sha1 = report['sha1']
            save = True
        if self.obj.sha256 != report['sha256']:
            self.obj.sha256 = report['sha256']
            save = True

        if save:
            self.obj.save(username=self.current_task.username)
        return None

    def _process_public_sample(self, report):
        """
        Process public key sample data from VirusTotal.

        Args:
            report (dict): json report information

        Return: dict with keys:
            'success' (boolean),
            'message' (str),
            'scandate' (str), if available
        """
        status = {
            'success':  False,
            'message':  []
        }

        # Checks to see if VT verbose_msg is provided. If not exit and return message
        vtmsg = report.get('verbose_msg', False)
        if not vtmsg:
            status['message'].append("No verbose message provided by VT.")

        # Add VT header information. Composed of information overview and permalink
        stats = {
            'scan_date':        report.get('scan_date', ''),
            'positives':        report.get('positives', 0),
            'total':            report.get('total', 0),
        }
        result_string = "%d / %d" % (report.get('positives', 0), report.get('total', 0))
        status['scandate'] = stats['scan_date']
        self._add_result('stats', result_string, stats)
        self._add_result('permalink', report.get("permalink", "No link"))

        # Add VT scan data
        scans = report.get('scans', [])
        if scans:
            for scan in scans:
                if scans[scan]["result"]:
                    result = scans[scan]["result"]
                else:
                    result = ''
                detection = {
                    "engine":       scan,
                    "date":         scans[scan].get('update', ''),
                    "detected":     scans[scan].get('detected', ''),
                    "version":      scans[scan].get('version', ''),
                }
                self._add_result('av result', result, detection)
        else:
            status['message'].append("Scan data not included in VT response.")

        # Updating status information and returning
        if not status['message']:
            status['success'] =  True
            status['message'] = "Processed Public Report Information."
        else:
            status['message'] = "\n".join(status['message'])

        return status

    def _process_private_sample_metadata(self, report):
        """
        Process private sample report information focused on file meta data
        from VirusTotal. This includes the section:
            - Developer Data
            - PE Language
            - Signature Check
        Args:
            report (dict): unprocessed main json report information from VT.

        Return: dict with keys:
            'success' (boolean),
            'message' (str),
        """
        status = {
            'success':  False,
            'message':  []
        }
        additional_info_dict = report.get('additional_info', {})
        exiftool_dict = additional_info_dict.get('exiftool', {})
        sigcheck_dict = additional_info_dict.get('sigcheck', {})

        # Developer Metadata
        if exiftool_dict:
            developerdata = {
                'Product Name': exiftool_dict.get('ProductName', ''),
                'Product Version': exiftool_dict.get('ProductVersionNumber', ''),
                'File Version': exiftool_dict.get('FileVersionNumber', ''),
                'File Description': exiftool_dict.get('FileDescription', ''),
                'InternalName': exiftool_dict.get('InternalName', ''),
            }
            # Make sure at least one of the keys is set with a value.
            for v in developerdata.values():
                if v != '':
                    self._add_result('Developer Metadata', exiftool_dict.get('CompanyName', ''), developerdata)
                    break
        else:
            status['message'].append("Exiftool data not included in VT response.")

        # Signature Information
        if sigcheck_dict:
            sigcheck = {
                'Copyright': sigcheck_dict.get('copyright', ''),
                'Description': sigcheck_dict.get('description', ''),
                'File Version': sigcheck_dict.get('file version', ''),
                'Internal Name': sigcheck_dict.get('internal name', ''),
                'Original Name': sigcheck_dict.get('original name', ''),
                'Product': sigcheck_dict.get('product', ''),
                'Link Date': sigcheck_dict.get('link date', ''),
                'Publisher': sigcheck_dict.get('publisher', ''),
                'Signers': sigcheck_dict.get('signers', ''),
                'Signing Date': sigcheck_dict.get('signing date', '')
            }
            # Make sure at least one of the keys is set with a value.
            for v in sigcheck.values():
                if v != '':
                    self._add_result('Signature Information', sigcheck_dict.get('publisher', ''), sigcheck)
                    break
        else:
            status['message'].append("Signature data not included in VT response.")

        # PE Language Informaton
        pe_lang = additional_info_dict.get('pe-resource-langs', {})
        if pe_lang:
            for k, v in pe_lang.iteritems():
                self._add_result('Language Information', k, {'Value': v})
        else:
            status['message'].append("PE Language data not included in VT response.")

        # Updating status information and returning
        if not status['message']:
            status['success'] =  True
            status['message'] = "Processed private sample metadata."
        else:
            status['message'] = "\n".join(status['message'])

        return status

    def _process_private_sample_vtmetadata(self, report):
        """
        Process private sample report information focused on virustotal's
        metadata. This includes the section:
            - VirusTotal Metadata (times seen)
            - VirusTotal Reputation
        Args:
            report (dict): unprocessed main json report information from VT.

        Return: dict with keys:
            'success' (boolean),
            'message' (str),
        """
        status = {
            'success':  False,
            'message':  []
        }

        additional_info_dict = report.get('additional_info', {})

        # VirusTotal Metadata
        if additional_info_dict:
            vt_metadata = {
                'First Seen': report.get('first_seen', ''),
                'Last Seen': report.get('last_seen', ''),
                'Times Submitted': report.get('times_submitted', ''),
                'Unique Sources': report.get('unique_sources', '')
            }
            self._add_result('VirusTotal Timestamps', report.get('scan_id', ''), vt_metadata)
            for item in report.get('submission_names', []):
                self._add_result('VirusTotal Submission Names', item)

            # VirusTotal Reputation
            vt_reputation = {
                'Community Reputation': report.get('community_reputation', 0),
                'Harmless Votes':       report.get('harmless_votes', 0),
                'Malicious Votes':      report.get('malicious_votes', 0)
            }
            vt_reputation_string =  "%d / %d" % (report.get('malicious_votes', 0), report.get('harmless_votes', 0))
            self._add_result('VirusTotal Reputation', vt_reputation_string, vt_reputation)
        else:
            status['message'].append("Additional information not included in VT response.")


        # Updating status information and returning
        if not status['message']:
            status['success'] =  True
            status['message'] = "Processed private sample vtmetadata."
        else:
            status['message'] = "\n".join(status['message'])

        return status

    def _process_private_sample_behaviour(self, report, scandate):
        """
        Process private sample report information focused on virustotal's
        behaviour information. This includes the section:
            - VirusTotal Metadata (times seen)
            - VirusTotal Reputation
        Args:
            report (dict): unprocessed main json report information from VT.

        Return: dict with keys:
            'success' (boolean),
            'message' (str),
        """
        status = {
            'success':  False,
            'message':  []
        }
        additional_info_dict = report.get('additional_info', {})
        behaviour_dict = additional_info_dict.get('behaviour-v1', {})

        # VirusTotal Network Behavioral Data
        if behaviour_dict:
            behaviour_network_dict = behaviour_dict.get('network', {})

            if behaviour_network_dict:
                # Grab DNS data if available
                behaviour_network_dns = behaviour_network_dict.get('dns', [])
                if behaviour_network_dns:
                    for item in behaviour_network_dns:
#                        self._process_domain(item.get('hostname', ''), item.get('ip', ''), scandate)
                        # Add domain to CRITs
                        domain = item.get('hostname', None)
                        ip = item.get('ip', None)
                        self._add_result('VirusTotal Behaviour DNS', str(domain), {'IP_Address': str(ip)})
# if domain and self.config.get('vt_add_domains', False)
                        if domain:
                            self._process_domain(domain, ip, scandate)
                else:
                    status['message'].append("DNS behaviour information not included in VT response.")

                # Grab HTTP data if available
                behaviour_network_http = behaviour_network_dict.get('http', [])
                if behaviour_network_http:
                    for item in behaviour_network_http:
                        item_dict = {
                            'Method':       item.get('method', ''),
                            'User-agent':   item.get('user-agent', '')
                        }
                        self._add_result('VirusTotal Behaviour HTTP', item.get('url', ''), item_dict)
                else:
                    status['message'].append("HTTP behaviour information not included in VT response.")

                # Grab TCP data if available
                behaviour_network_tcp = behaviour_network_dict.get('tcp', [])
                if behaviour_network_tcp:
                    for item in behaviour_network_tcp:
                        self._add_result('VirusTotal Behaviour TCP', item)
                else:
                    status['message'].append("TCP behaviour information not included in VT response.")

                # Grab UDP data if available
                behaviour_network_udp = behaviour_network_dict.get('udp', [])
                if behaviour_network_udp:
                    for item in behaviour_network_udp:
                        self._add_result('VirusTotal Behaviour UDP', item)
                else:
                    status['message'].append("UDP behaviour information not included in VT response.")

            # VirusTotal Extra Flags
            behaviour_extra = behaviour_dict.get('extra', [])
            if behaviour_extra:
                for item in behaviour_extra:
                    self._add_result('VirusTotal Behaviour Flag', item, {'VT_Flag': 'VT_Flag'})
            else:
                status['message'].append("Behaviour flag information not included in VT response.")

            # VirusTotal Hooking Detection
            behaviour_hooking = behaviour_dict.get('hooking', [])
            if behaviour_hooking:
                for item in behaviour_hooking:
                    item_dict = {
                        'method':   item.get('method', ''),
                        'success':  item.get('success', '')
                    }
                    self._add_result('Virus Total Hooking Detected', item.get('type', ''), item_dict)
            else:
                status['message'].append("Hooking behaviour information not included in VT response.")

        else:
            status['message'].append("Behaviour information not included in VT response.")

        # Updating status information and returning
        if not status['message']:
            status['success'] =  True
            status['message'] = "Processed private sample behaviour data."
        else:
            status['message'] = "\n".join(status['message'])

        return status

    def _process_public_domain(self, report):
        """
        Process public key domain data from VirusTotal. This is a mess on VT's side. Standards....

        Args:
            report (dict): json report information

        Return: dict with keys:
            'success' (boolean),
            'message' (str),
        """
        status = {
            'success':  False,
            'message':  []
        }

        detected_urls = report.get('detected_urls', [])
        if detected_urls:
            for detected_url in detected_urls:
                stats = {
                          'scan_date': detected_url.get('scan_date', ''),
                          'total': detected_url.get('total', 0),
                          'positives': detected_url.get('positives', 0),
                        }
                self._add_result('URLs', detected_url.get('url', ''), stats)
        else:
            status['message'].append("URL information not included in VT response.")

        resolutions = report.get('resolutions', [])
        if resolutions:
            for resolution in resolutions:
                stats = { 'last_resolved': resolution.get('last_resolved', '') }
                self._add_result('A Records', resolution.get('ip_address', ''), stats)
        else:
            status['message'].append("Resolution information not included in VT response.")

        categories = report.get('categories', [])
        if categories:
            for category in categories:
                self._add_result('Categories', category, {})
        else:
            status['message'].append("Category information not included in VT response.")

        communicating_samples = report.get('detected_communicating_samples', [])
        if communicating_samples:
            for sample in communicating_samples:
                stats = {
                          'date': sample.get('date', ''),
                          'total': sample.get('total', 0),
                          'positives': sample.get('positives', 0),
                        }
                self._add_result('Detected Communicating Samples', sample.get('sha256', 0), stats)
        else:
            status['message'].append("Detected communicating sample information not included in VT response.")

        # This is added in the IP data from the original VT service. However I have not see this used. I am adding it
        # just incase.
        undetected_communicating_samples = report.get('undetected_communicating_samples', [])
        if undetected_communicating_samples:
            for sample in undetected_communicating_samples:
                stats = {
                          'date': sample.get('date', ''),
                          'total': sample.get('total', 0),
                          'positives': sample.get('positives', 0),
                        }
                self._add_result('Undetected Communicating Samples', sample.get('sha256', 0), stats)
        else:
            status['message'].append("Undetected communicating sample information not included in VT response.")

        downloaded_samples = report.get('detected_downloaded_samples', [])
        if downloaded_samples:
            for sample in downloaded_samples:
                stats = {
                          'date': sample.get('date', ''),
                          'total': sample.get('total', 0),
                          'positives': sample.get('positives', 0),
                        }
                self._add_result('Detected Downloaded Samples', sample.get('sha256', 0), stats)
        else:
            status['message'].append("Downloaded sample information not included in VT response.")

        undetected_downloaded_samples = report.get('undetected_downloaded_samples', [])
        if undetected_downloaded_samples:
            for sample in undetected_downloaded_samples:
                stats = {
                          'date': sample.get('date', ''),
                          'total': sample.get('total', 0),
                          'positives': sample.get('positives', 0),
                        }
                self._add_result('Undetected Downloaded Samples', sample.get('sha256', 0), stats)
        else:
            status['message'].append("Undetected domain sample information not included in VT response.")

        # Updating status information and returning
        if not status['message']:
            status['success'] =  True
            status['message'] = "Processed public domain information."
        else:
            status['message'] = "\n".join(status['message'])

        return status

    def _process_public_ip(self, report):
        """
        Process public key ip data from VirusTotal.

        Args:
            report (dict): json report information

        Return: dict with keys:
            'success' (boolean),
            'message' (str),
        """
        status = {
            'success':  False,
            'message':  []
        }

        detected_urls = report.get('detected_urls', [])
        if detected_urls:
            for url in detected_urls:
                stats = {
                          'scan_date': url.get('scan_date', ''),
                          'total': url.get('total', 0),
                          'positives': url.get('positives', 0)
                        }
                self._add_result('Detected URLs', url.get('url', ''), stats)
        else:
            status['message'].append("Detected URL information not included in VT response.")

        resolutions = report.get('resolutions', [])
        if resolutions:
            for resolution in resolutions:
                stats = {
                          'last_resolved': resolution.get('last_resolved', ''),
                        }
                self._add_result('Resolutions', resolution.get('hostname', ''), stats)
        else:
            status['message'].append("Resolution information not included in VT response.")

        detected_communicating_samples = report.get('detected_communicating_samples', [])
        if detected_communicating_samples:
            for samp in detected_communicating_samples:
                stats = {
                          'date': samp.get('date', ''),
                          'total': samp.get('total', 0),
                          'positives': samp.get('positives', 0)
                        }
                self._add_result('Detected Communicating Samples', samp.get('sha256', ''), stats)
        else:
            status['message'].append("Detected communicating sample information not included in VT response.")

        undetected_communicating_samples = report.get('undetected_communicating_samples', [])
        if undetected_communicating_samples:
            for samp in undetected_communicating_samples:
                stats = {
                          'date': samp.get('date', ''),
                          'total': samp.get('total', 0),
                          'positives': samp.get('positives', 0)
                        }
                self._add_result('Undetected Communicating Samples', samp.get('sha256', ''), stats)
        else:
            status['message'].append("Undetected communicating sample information not included in VT response.")

        detected_downloaded_samples = report.get('detected_downloaded_samples', [])
        if detected_downloaded_samples:
            for samp in detected_downloaded_samples:
                stats = {
                          'date': samp.get('date', ''),
                          'total': samp.get('total', 0),
                          'positives': samp.get('positives', 0)
                        }
                self._add_result('Detected Downloaded Samples', samp.get('sha256', ''), stats)
        else:
            status['message'].append("Detected downloaded sample information not included in VT response.")

        undetected_downloaded_samples = report.get('undetected_downloaded_samples', [])
        if undetected_downloaded_samples:
            for samp in undetected_downloaded_samples:
                stats = {
                          'date': samp.get('date', ''),
                          'total': samp.get('total', 0),
                          'positives': samp.get('positives', 0)
                        }
                self._add_result('Undetected Downloaded Samples', samp.get('sha256', ''), stats)
        else:
            status['message'].append("Undetected downloaded sample information not included in VT response.")

        # Updating status information and returning
        if not status['message']:
            status['success'] =  True
            status['message'] = "Processed public ip information."
        else:
            status['message'] = "\n".join(status['message'])

        return status

    def _process_pcap(self, pcap, scandate):
        """
        Add Pcap file to CRITs.

        Args:
            pcap (binary): pcap data
            scandate (str): scan date from when pcap was collected

        TODO:
            Add an error check
        """
        self._info("Adding PCAP and creating relationship to %s" % (str(self.obj.id)))
        self._notify()
        h = md5(pcap).hexdigest()
        result = handle_pcap_file("%s.pcap" % h,                            # File Name
                                  pcap,                                     # Pcap data
                                  self.obj.source,                                      # Data Source
                                  user=self.current_task.username,          # User adding the PCAP
                                  description='Created %s' % (scandate),    # Description
                                  related_id=str(self.obj.id),              # Top level ID of related object
                                  related_type="Sample",                    # Top level type of the related object
                                  method=self.name,                         # Method for aquiring the PCAP
                                  reference=None,                           # Reference to the source of this PCAP
                                  relationship='Related_To')                # Relationship between parent and the PCAP

        self._add_result("pcap added", h, {'md5': h})

def _process_domain(self, domain, ip, scandate):
        """
        Add domain to CRITs.
        Args:
            domain (str): pcap data
            scandate (str): scan date from when domain is believed to be
            collected.
        TODO:
            handle IP
        """

        self._info("Adding domain %s and creating relationship to %s" % (str(domain), str(self.obj.id)))
        self._notify()

        result = upsert_domain(domain,
                               self.obj.source,
                               username=self.current_task.username,
                               campaign=None,
                               confidence=None,
                               bucket_list=None,
                               ticket=None)

        # If domain was added, create relationship.
        if not result['success']:
            self._info("Cannot add domain %s. reason: %s" % (str(domain), str(result['message'])))
        else:
            # add relationshiop
            dmain = result['object']

            msg = dmain.add_relationship(rel_item=self.obj,
                                         rel_type='Related_To',
                                         rel_date=scandate,
                                         analyst=self.current_task.username,
                                         rel_confidence='unknown',
                                         rel_reason='Provided by VirusTotal. Date is from when vt analysis was performed',
                                         get_rels=False)

            if not msg['success']:
                self._info("Cannot add relationship because %s" % (str(msg['message'])))

            dmain.save(username=self.current_task.username)
            self.obj.save(username=self.current_task.username)
