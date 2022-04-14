# -*- coding: UTF-8 -*-
import json
import oss2
import requests
import time

from voduploadsdk.AliyunVodUtils import *


VOD_MAX_TITLE_LENGTH = 128
VOD_MAX_DESCRIPTION_LENGTH = 1024

class AliyunVodUploader:
    
    def __init__(self, accessKeyId, ecsRegionId=None):
        """
        constructor for VodUpload
        :param accessKeyId: string, access key id
        :param accessKeySecret: string, access key secret
        :param ecsRegion: string, 部署迁移脚本的ECS所在的Region，详细参考：https://help.aliyun.com/document_detail/40654.html，如：cn-beijing
        :return
        """
        self.__accessKeyId = accessKeyId
        self.__accessKeySecret = None
        self.__ecsRegion = ecsRegionId
        self.__vodApiRegion = None
        self.__connTimeout = 3
        self.__bucketClient = None
        self.__maxRetryTimes = 3
        self.__vodClient = None
        self.__EnableCrc = True
        self.__credential = None
        # 分片上传参数
        self.__multipartThreshold = 10 * 1024 * 1024    # 分片上传的阈值，超过此值开启分片上传
        self.__multipartPartSize = 10 * 1024 * 1024     # 分片大小，单位byte
        self.__multipartThreadsNum = 3                  # 分片上传时并行上传的线程数，暂时为串行上传，不支持并行，后续会支持。

        self.setApiRegion('cn-shanghai', self.__credential)


    def setApiRegion(self, apiRegion, credential):
        """
        设置VoD的接入地址，中国大陆为cn-shanghai，海外支持ap-southeast-1(新加坡)等区域，详情参考：https://help.aliyun.com/document_detail/98194.html
        :param apiRegion: 接入地址的Region英文表示
        :return:
        """
        self.__vodApiRegion = apiRegion

    def setMultipartUpload(self, multipartThreshold=10*1024*1024, multipartPartSize=10*1024*1024, multipartThreadsNum=1):
        if multipartThreshold > 0:
            self.__multipartThreshold = multipartThreshold
        if multipartPartSize > 0:
            self.__multipartPartSize = multipartPartSize
        if multipartThreadsNum > 0:
            self.__multipartThreadsNum = multipartThreadsNum

    def setEnableCrc(self, isEnable=False):
        self.__EnableCrc = True if isEnable else False
    
    @catch_error
    def uploadLocalVideo(self, uploadInfo, filePath):
        """
        上传本地视频或音频文件到点播，最大支持48.8TB的单个文件，暂不支持断点续传
        :param startUploadCallback为获取到上传地址和凭证(uploadInfo)后开始进行文件上传时的回调，可用于记录上传日志等；uploadId为设置的上传ID，可用于关联导入视频。
        :return
        """
        self.__uploadOssObjectWithRetry(filePath, uploadInfo['UploadAddress']['FileName'], uploadInfo)
        return uploadInfo['VideoId']

    # 定义进度条回调函数；consumedBytes: 已经上传的数据量，totalBytes：总数据量
    def uploadProgressCallback(self, consumedBytes, totalBytes):
    
        if totalBytes:
            rate = int(100 * (float(consumedBytes) / float(totalBytes)))
        else:
            rate = 0
              
        print ("[%s]uploaded %s bytes, percent %s%s" % (AliyunVodUtils.getCurrentTimeStr(), consumedBytes, format(rate), '%'))
        sys.stdout.flush()

    # uploadType，可选：multipart, put, web
    def __uploadOssObjectWithRetry(self, filePath, object, uploadInfo, headers=None):
        retryTimes = 0
        while retryTimes < self.__maxRetryTimes:
            try:
                return self.__uploadOssObject(filePath, object, uploadInfo, headers)
            except OssError as e:
                # 上传凭证过期需要重新获取凭证
                if e.code == 'SecurityTokenExpired' or e.code == 'InvalidAccessKeyId':
                    #uploadInfo = self.__refresh_upload_video(uploadInfo['MediaId'])
                    raise e
            except Exception as e:
                raise e
            except:
                raise AliyunVodException('UnkownError', repr(e), traceback.format_exc())
            finally:
                retryTimes += 1
            
        
    def __uploadOssObject(self, filePath, object, uploadInfo, headers=None):
        self.__createOssClient(uploadInfo['UploadAuth'], uploadInfo['UploadAddress'])
        """
        p = os.path.dirname(os.path.realpath(__file__))
        store = os.path.dirname(p) + '/osstmp'
        return oss2.resumable_upload(self.__bucketClient, object, filePath,
                              store=oss2.ResumableStore(root=store), headers=headers,
                              multipart_threshold=self.__multipartThreshold, part_size=self.__multipartPartSize,
                              num_threads=self.__multipartThreadsNum, progress_callback=self.uploadProgressCallback)
        """
        uploader = _VodResumableUploader(self.__bucketClient, filePath, object, uploadInfo, headers,self.uploadProgressCallback)
                                         #self.uploadProgressCallback, self.__refreshUploadAuth)
        uploader.setMultipartInfo(self.__multipartThreshold, self.__multipartPartSize, self.__multipartThreadsNum)
        uploader.setClientId(self.__accessKeyId)
        res = uploader.upload()

        uploadAddress = uploadInfo['UploadAddress']
        bucketHost = uploadAddress['Endpoint'].replace('://', '://' + uploadAddress['Bucket'] + ".")
        logger.info("UploadFile %s Finish, MediaId: %s, FilePath: %s, Destination: %s/%s" % (
            uploadInfo['MediaType'], uploadInfo['MediaId'], filePath, bucketHost, object))
        return res
        
    # 使用上传凭证和地址信息初始化OSS客户端（注意需要先Base64解码并Json Decode再传入）
    # 如果上传的ECS位于点播相同的存储区域（如上海），则可以指定internal为True，通过内网上传更快且免费
    def __createOssClient(self, uploadAuth, uploadAddress):
        auth = oss2.StsAuth(uploadAuth['AccessKeyId'], uploadAuth['AccessKeySecret'], uploadAuth['SecurityToken'])
        endpoint = AliyunVodUtils.convertOssInternal(uploadAddress['Endpoint'], self.__ecsRegion)
        self.__bucketClient = oss2.Bucket(auth, endpoint, uploadAddress['Bucket'],
                                          connect_timeout=self.__connTimeout, enable_crc=self.__EnableCrc)
        return self.__bucketClient

from oss2 import SizedFileAdapter, determine_part_size
from oss2.models import PartInfo
from aliyunsdkcore.utils import parameter_helper as helper
class _VodResumableUploader:
    def __init__(self, bucket, filePath, object, uploadInfo, headers, progressCallback):
        self.__bucket = bucket
        self.__filePath = filePath
        self.__object = object
        self.__uploadInfo = uploadInfo
        self.__totalSize = None
        self.__headers = headers
        self.__mtime = os.path.getmtime(filePath)
        self.__progressCallback = progressCallback

        self.__threshold = None
        self.__partSize = None
        self.__threadsNum = None
        self.__uploadId = 0

        self.__record = {}
        self.__finishedSize = 0
        self.__finishedParts = []
        self.__filePartHash = None
        self.__clientId = None

    def setMultipartInfo(self, threshold, partSize, threadsNum):
        self.__threshold = threshold
        self.__partSize = partSize
        self.__threadsNum = threadsNum


    def setClientId(self, clientId):
        self.__clientId = clientId


    def upload(self):
        self.__totalSize = os.path.getsize(self.__filePath)
        if self.__threshold and self.__totalSize <= self.__threshold:
            return self.simpleUpload()
        else:
            return self.multipartUpload()


    def simpleUpload(self):
        with open(AliyunVodUtils.toUnicode(self.__filePath), 'rb') as f:
            result = self.__bucket.put_object(self.__object, f, headers=self.__headers, progress_callback=None)
            if self.__uploadInfo['MediaType'] == 'video':
                self.__reportUploadProgress('put', 1, self.__totalSize)

            return result

    def multipartUpload(self):
        psize = oss2.determine_part_size(self.__totalSize, preferred_size=self.__partSize)
        
        # 初始化分片
        self.__uploadId = self.__bucket.init_multipart_upload(self.__object).upload_id

        startTime = time.time()
        expireSeconds = 2500    # 上传凭证有效期3000秒，提前刷新
        # 逐个上传分片
        with open(AliyunVodUtils.toUnicode(self.__filePath), 'rb') as fileObj:
            partNumber = 1
            offset = 0

            while offset < self.__totalSize:
                uploadSize = min(psize, self.__totalSize - offset)
                #logger.info("UploadPart, FilePath: %s, VideoId: %s, UploadId: %s, PartNumber: %s, PartSize: %s" % (self.__fileName, self.__videoId, self.__uploadId, partNumber, uploadSize))
                result = self.__bucket.upload_part(self.__object, self.__uploadId, partNumber, SizedFileAdapter(fileObj,uploadSize))
                #print(result.request_id)
                self.__finishedParts.append(PartInfo(partNumber, result.etag))
                offset += uploadSize
                partNumber += 1

                # 上传进度回调
                self.__progressCallback(offset, self.__totalSize)

                if self.__uploadInfo['MediaType'] == 'video':
                    # 上报上传进度
                    self.__reportUploadProgress('multipart', partNumber - 1, offset)

                    # 检测上传凭证是否过期
                    nowTime = time.time()
                    # if nowTime - startTime >= expireSeconds:
                    #     self.__bucket = self.__refreshAuthCallback(self.__uploadInfo['MediaId'])
                    #     startTime = nowTime


        # 完成分片上传
        self.__bucket.complete_multipart_upload(self.__object, self.__uploadId, self.__finishedParts, headers=self.__headers)
        
        return result


    def __reportUploadProgress(self, uploadMethod, donePartsCount, doneBytes):
        reportHost = 'vod.cn-shanghai.aliyuncs.com'
        sdkVersion = '1.3.1'
        reportKey = 'HBL9nnSwhtU2$STX'

        uploadPoint = {'upMethod': uploadMethod, 'partSize': self.__partSize, 'doneBytes': doneBytes}
        timestamp = int(time.time())
        authInfo = AliyunVodUtils.getStringMd5("%s|%s|%s" % (self.__clientId, reportKey, timestamp))

        fields = {'Action': 'ReportUploadProgress', 'Format': 'JSON', 'Version': '2017-03-21',
                'Timestamp': helper.get_iso_8061_date(), 'SignatureNonce': helper.get_uuid(),
                'VideoId': self.__uploadInfo['MediaId'], 'Source': 'PythonSDK', 'ClientId': self.__clientId,
                'BusinessType': 'UploadVideo', 'TerminalType': 'PC', 'DeviceModel': 'Server',
                'AppVersion': sdkVersion, 'AuthTimestamp': timestamp, 'AuthInfo': authInfo,

                'FileName': self.__filePath, 'FileHash': self.__getFilePartHash(self.__clientId, self.__filePath, self.__totalSize),
                'FileSize': self.__totalSize, 'FileCreateTime': timestamp, 'UploadRatio': 0, 'UploadId': self.__uploadId,
                'DonePartsCount': donePartsCount, 'PartSize': self.__partSize, 'UploadPoint': json.dumps(uploadPoint),
                'UploadAddress': self.__uploadInfo['OriUploadAddress']
        }
        requests.post('http://' + reportHost, fields, timeout=1)


    def __getFilePartHash(self, clientId, filePath, fileSize):
        if self.__filePartHash:
            return self.__filePartHash

        length = 1 * 1024 * 1024
        if fileSize < length:
            length = fileSize

        try:
            fp = open(AliyunVodUtils.toUnicode(filePath), 'rb')
            strVal = fp.read(length)
            self.__filePartHash = AliyunVodUtils.getStringMd5(strVal, False)
            fp.close()
        except:
            self.__filePartHash = "%s|%s|%s" % (clientId, filePath, self.__mtime)

        return self.__filePartHash
