#!/usr/bin/env python
# __BEGIN_LICENSE__
#  Copyright (c) 2009-2013, United States Government as represented by the
#  Administrator of the National Aeronautics and Space Administration. All
#  rights reserved.
#
#  The NGT platform is licensed under the Apache License, Version 2.0 (the
#  "License"); you may not use this file except in compliance with the
#  License. You may obtain a copy of the License at
#  http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
# __END_LICENSE__

# Process an entire run of icebrige images. Multiple runs will be started in parallel.

# All the image, camera, and lidar files must have date and time stamps,
# like the orthoimages and the Fireball DEMs. As such, raw image
# files must be renamed to be similar to the ortho image files.
# No other files must be present in those directries.
# Image files must be single-channel, so use for example gdal_translate -b 1.

import os, sys, optparse, datetime, multiprocessing, time, logging
import os.path as P

# The path to the ASP python files
basepath    = os.path.abspath(sys.path[0])
pythonpath  = os.path.abspath(basepath + '/../Python')  # for dev ASP
libexecpath = os.path.abspath(basepath + '/../libexec') # for packaged ASP
icebridgepath = os.path.abspath(basepath + '/../IceBridge')  # IceBridge tools
toolspath = os.path.abspath(basepath + '/../Tools')  # ASP Tools
sys.path.insert(0, basepath) # prepend to Python path
sys.path.insert(0, pythonpath)
sys.path.insert(0, libexecpath)
sys.path.insert(0, icebridgepath)
sys.path.insert(0, libexecpath)
sys.path.insert(0, toolspath)

import icebridge_common, process_icebridge_batch
import asp_system_utils, asp_alg_utils, asp_geo_utils
asp_system_utils.verify_python_version_is_supported()

logger = logging.getLogger(__name__)

# Prepend to system PATH
os.environ["PATH"] = libexecpath + os.pathsep + os.environ["PATH"]
os.environ["PATH"] = toolspath   + os.pathsep + os.environ["PATH"]

# This is the file name in the output folder where batch commands will
#  be written to with the --log-batches option.
BATCH_COMMAND_LOG_FILE = 'batch_commands_log.txt'

def processBatch(imageCameraPairs, lidarFolder, outputFolder, extraOptions, 
                 batchNum, batchLogPath=''):
    '''Processes a batch of images at once'''

    suppressOutput = False
    redo           = False

    argString = ''
    for pair in imageCameraPairs:
        argString += pair[0] + ' '
    for pair in imageCameraPairs:
        argString += pair[1] + ' '

    # Just set the options and call the pair python tool.
    # We can try out bundle adjustment for intrinsic parameters here.
    cmd = ('--lidar-overlay --lidar-folder %s %s %s %s' 
           % (lidarFolder, outputFolder, argString, extraOptions))
    
    if batchLogPath:
        # With this option we just log the commands to a text file
        # - Setting this option limits to one process so there will be only one 
        #   simultaneous file writer.
        with open(batchLogPath, 'a') as f:
            # Prepend the batch number to each line
            f.write(str(batchNum)+': '+ cmd + '\n')
        return
    
    try:
        process_icebridge_batch.main(cmd.split())
    except Exception as e:
        logger.error('Pair processing failed!\n' + str(e))

def getImageSpacing(orthoFolder):
    '''Find a good image stereo spacing interval that gives us a good
       balance between coverage and baseline width.
       Also detect all frames where this is a large break after the current frame.'''

    # Do nothing if this option was not provided
    if not orthoFolder:
        return None
   
    logger.info('Computing optimal image stereo interval...')

    breaks = []
   
    # Generate a list of valid, full path ortho files
    fileList = os.listdir(orthoFolder)
    orthoFiles = []
    for orthoFile in fileList:     
        # Skip non-image files (including junk from stereo_gui) and duplicate grayscale files
        ext = os.path.splitext(orthoFile)[1]
        if (ext != '.tif') or ('_sub' in orthoFile) or ('.tif_gray.tif' in orthoFile):
            continue
        orthoPath = os.path.join(orthoFolder, orthoFile)
        orthoFiles.append(orthoPath)
    orthoFiles.sort()
    numOrthos = len(orthoFiles)

    # Get the bounding box and frame number of each ortho image
    logger.info('Loading bounding boxes...')
    bboxes = []
    frames = []
    for i in range(0, numOrthos):
        imageGeoInfo = asp_geo_utils.getImageGeoInfo(orthoFiles[i], getStats=False)
        thisBox      = imageGeoInfo['projection_bounds']
        thisFrame    = icebridge_common.getFrameNumberFromFilename(orthoFiles[i])
        bboxes.append(thisBox)
        frames.append(thisFrame)

    # Since we are only comparing the image bounding boxes, not their exact corners,
    #  these ratios are only estimates.
    MAX_RATIO = 0.8 # Increase skip until we get below this...
    MIN_RATIO = 0.4 # ... but don't go below this value!

    def getBboxArea(bbox):
        '''Return the area of a bounding box in form of (minX, maxX, minY, maxY)'''
        width  = bbox[1] - bbox[0]
        height = bbox[3] - bbox[2]
        if (width < 0) or (height < 0):
            return 0
        return width*height

    # Iterate over stereo image intervals to try to get our target numbers
    interval  = 0
    meanRatio = 1.0
    while meanRatio > MAX_RATIO:
        meanRatio = 0
        count     = 0
        interval  = interval + 1
        logger.info('Trying stereo image interval ' + str(interval))

        if numOrthos <= interval:
            raise Exception('Error: There are too few images and they overlap too much. ' + \
                            'Consider processing more images in the given batch.')       

        for i in range(0, numOrthos-interval):
            
            # Compute intersection area between this and next image
            thisBox   = bboxes[i  ]
            lastBox   = bboxes[i+interval]
            intersect = [max(lastBox[0], thisBox[0]), # Min X
                         min(lastBox[1], thisBox[1]), # Max X
                         max(lastBox[2], thisBox[2]), # Min Y
                         min(lastBox[3], thisBox[3])] # Max Y
            thisArea  = getBboxArea(thisBox)
            area      = getBboxArea(intersect)
            ratio     = area / thisArea

            # Don't include non-overlapping frames in the statistics
            if area > 0:
                meanRatio = meanRatio + ratio
                count     = count + 1
            
            # On the first pass (with interval 1) check for gaps in coverage.
            if (interval == 1) and (area <= 0):
                breaks.append(frames[i])
                logger.info('Detected large break after frame ' + str(frames[i]))
            
        # Get the mean intersection ratio
        meanRatio = meanRatio / count
        logger.info('  --> meanRatio = ' + str(meanRatio))

    # If we increased the interval too much, back it off by one step.
    if (meanRatio < MIN_RATIO) and (interval > 1):
        interval = interval - 1
        
    logger.info('Computed automatic image stereo interval: ' + str(interval))
    logger.info('Detected ' + str(interval) + ' breaks in image coverage.')
    
    return (interval, breaks)


def main(argsIn):

    try:
        usage = '''usage: process_icebridge_run.py <image_folder> <camera_folder>
                      <lidar_folder> <output_folder>'''
                      
        parser = optparse.OptionParser(usage=usage)

        # Data selection optios
        parser.add_option('--start-frame', dest='startFrame', default=-1,
                          type='int', help='The frame number to start processing with.')
        parser.add_option('--stop-frame', dest='stopFrame', default=-1,
                          type='int', help='The frame number to finish processing with.')        
        parser.add_option('--south', action='store_true', default=False, dest='isSouth',  
                          help='MUST be set if the images are in the southern hemisphere.')

        # Processing options
        parser.add_option('--stereo-algorithm', dest='stereoAlgo', default=1,
                          type='int', help='The SGM stereo algorithm to use.')
        parser.add_option('--subpixel-mode', dest='subpix_mode', default=1,
                          type='int', help='Subpixel mode (1 = fast but low quality, 3 = slow). Only applicable for non-SGM runs.')

        parser.add_option('--bundle-length', dest='bundleLength', default=2,
                          type='int', help='Number of images to bundle adjust and process at once.')
        parser.add_option('--image-stereo-interval', dest='imageStereoInterval', default=None,
                          type='int', help='Advance this many frames to get the stereo pair.  Default is auto-calculate')

        parser.add_option('--solve-intrinsics', action='store_true', default=False,
                          dest='solve_intr',  
                          help='If to float the intrinsics params.')

        #parser.add_option('--dem-resolution', dest='demResolution', default=0.4,
        #                  type='float', help='Generate output DEMs at this resolution.')

        parser.add_option('--max-displacement', dest='maxDisplacement', default=20,
                          type='float', help='Max displacement value passed to pc_align.')


        # Performance options
        parser.add_option('--num-processes', dest='numProcesses', default=1,
                          type='int', help='The number of simultaneous processes to run.')
        parser.add_option('--num-threads', dest='numThreads', default=None,
                          type='int', help='The number threads to use per process.')                         

        # Action options
        parser.add_option('--interactive', action='store_true', default=False, dest='interactive',  
                          help='If to wait on user input to terminate the jobs.')
        parser.add_option('--dry-run', action='store_true', default=False, dest='dryRun',  
                          help="Print but don't launch the processing jobs.")
        parser.add_option('--log-batches', action='store_true', default=False, dest='logBatches',  
                          help="Just log the batch commands to a file.")


        parser.add_option('--ortho-folder', dest='orthoFolder', default=None,
                          help='Use ortho files to adjust processing to the image spacing.')


        (options, args) = parser.parse_args(argsIn)

        if len(args) < 4:
            logger.info(usage)
            return 0

        imageFolder  = args[0]
        cameraFolder = args[1]
        lidarFolder  = args[2]
        outputFolder = args[3]

    except optparse.OptionError, msg:
        raise Usage(msg)
    
    # Check the inputs
    for f in [imageFolder, cameraFolder, lidarFolder]:
        if not os.path.exists(f):
            logger.error('Input file '+ f +' does not exist!')
            return 0
    if not os.path.exists(outputFolder):
        os.mkdir(outputFolder)

    suppressOutput = False
    redo           = False

    logger.info('\nStarting processing...')
    
    # Get a list of all the input files
    imageFiles  = os.listdir(imageFolder)
    cameraFiles = os.listdir(cameraFolder)
    # Filter the file types
    imageFiles  = [f for f in imageFiles  if (os.path.splitext(f)[1] == '.tif') and ('_sub' not in f)] 
    cameraFiles = [f for f in cameraFiles if os.path.splitext(f)[1] == '.tsai']
    imageFiles.sort() # Put in order so the frames line up
    cameraFiles.sort()
    imageFiles  = [os.path.join(imageFolder, f) for f in imageFiles ] # Get full paths
    cameraFiles = [os.path.join(cameraFolder,f) for f in cameraFiles]

    numFiles = len(imageFiles)
    if (len(cameraFiles) != numFiles):
        print imageFiles
        print cameraFiles
        logger.error('process_icebridge.py: counted ' + str(len(imageFiles)) + ' image files.\n'+
                     'and ' + str(len(cameraFiles)) + ' camera files.\n'+
                     'Error: Number of image files and number of camera files must match!')
        return -1
        
    imageCameraPairs = zip(imageFiles, cameraFiles)
    
    # Check that the files are properly aligned
    for (image, camera) in imageCameraPairs: 
        frameNumber = icebridge_common.getFrameNumberFromFilename(image)
        if (icebridge_common.getFrameNumberFromFilename(camera) != frameNumber):
          logger.error('Error: input files do not align!\n' + str((image, camera)))
          return -1
        
    # Generate a map of initial camera positions
    orbitvizBefore = os.path.join(outputFolder, 'cameras_in.kml')
    vizString  = ''
    for (image, camera) in imageCameraPairs: 
        vizString += image +' ' + camera+' '
    cmd = 'orbitviz --hide-labels -t nadirpinhole -r wgs84 -o '+ orbitvizBefore +' '+ vizString
    logger.info(cmd)
    asp_system_utils.executeCommand(cmd, orbitvizBefore, suppressOutput, redo)
    
    # Set up options for process_icebridge_batch
    extraOptions = ' --stereo-algorithm ' + str(options.stereoAlgo)
    if options.numThreads:
        extraOptions += ' --num-threads ' + str(options.numThreads)
    if options.solve_intr:
        extraOptions += ' --solve-intrinsics '
    if options.isSouth:
        extraOptions += ' --south '
    if options.maxDisplacement:
        extraOptions += ' --max-displacement ' + str(options.maxDisplacement)
   
    (autoStereoInterval, breaks) = getImageSpacing(options.orthoFolder)
    if options.imageStereoInterval: 
        logger.info('Using manually specified image stereo interval: ' + str(options.imageStereoInterval))
    else:
        logger.info('Using automatic stereo interval: ' + str(autoStereoInterval))
        options.imageStereoInterval = autoStereoInterval
        if options.imageStereoInterval >= numFiles:
            raise Exception('Error: Automatic skip interval is greater than the number of input files!')       
    extraOptions += ' --stereo-image-interval ' + str(options.imageStereoInterval)

    logger.info('Detected frame breaks: ' + str(breaks))

    sleepTime = 20

    # If all we are doing is logging commands then one process is sufficient.
    # - Wipe the output file while we are at it.
    batchLogPath = ''
    batchNum = 0
    if options.logBatches:
        options.numProcesses = 1
        sleepTime    = 1
        batchLogPath = os.path.join(outputFolder, BATCH_COMMAND_LOG_FILE)
        os.system('rm -f ' + batchLogPath)
        logger.info('Just generating batch log file '+batchLogPath+', no processing will occur.')

    logger.info('Starting processing pool with ' + str(options.numProcesses) +' processes.')
    pool = multiprocessing.Pool(options.numProcesses)
    
    # Call process_icebridge_batch on each batch of images.
    # - Batch size should be the largest number of images which can be effectively bundle-adjusted.
    taskHandles           = []
    batchImageCameraPairs = []
    frameNumbers          = []
    for i in range(0,numFiles):
    
        # Check if this is inside the user specified frame range
        frameNumber = icebridge_common.getFrameNumberFromFilename(imageCameraPairs[i][0])
        if options.startFrame and (frameNumber < options.startFrame):
            continue
        if options.stopFrame and (frameNumber > options.stopFrame):
            continue
        if not options.logBatches:
            logger.info('Processing frame number: ' + str(frameNumber))

        # Add frame to the list for the current batch
        batchImageCameraPairs.append(imageCameraPairs[i])
        frameNumbers.append(frameNumber)
        
        numPairs = len(batchImageCameraPairs)
        
        # Keep adding frames until we get enough or hit the last frame or hit a break
        hitBreakFrame = frameNumber in breaks
        if (numPairs < options.bundleLength) and (frameNumber < options.stopFrame) and (not hitBreakFrame):
            continue

        # Check if the output file already exists.
        #thisDemFile      = os.path.join(thisOutputFolder, 'DEM.tif')
        #if os.path.exists(thisDemFile):
        #    print("Skipping frame as file exists: " + thisDemFile)
        #    continue
          
        # The output folder is named after the first and last frame in the batch
        thisOutputFolder = os.path.join(outputFolder, 'batch_'+str(frameNumbers[0])+'_'+str(frameNumbers[-1]))

        if not options.logBatches:
            logger.info('Running processing batch in output folder: ' + thisOutputFolder + '\n' + 
                        'with options: ' + extraOptions)
        
        if not options.dryRun:
            # Generate the command call
            taskHandles.append(pool.apply_async(processBatch, 
                (batchImageCameraPairs, lidarFolder, thisOutputFolder, extraOptions, 
                 batchNum, batchLogPath)))
        batchNum += 1
        
        if hitBreakFrame:
            # When we hit a break in the frames we need to start the next batch after the break frame
            batchImageCameraPairs = []
            frameNumbers          = []
        else:
            # Reset variables to start from a frame near the end of the current set.
            # - The amount of frame overlap is equal to the image stereo interval,
            #   this makes it so that that each image gets to be the left image in a stereo pair.
            batchOverlapCount     = -1 * options.imageStereoInterval
            batchImageCameraPairs = batchImageCameraPairs[batchOverlapCount:]
            frameNumbers          = frameNumbers[batchOverlapCount:]
            
    # End of loop through input file pairs
    logger.info('Finished adding ' + str(len(taskHandles)) + ' tasks to the pool.')
    
    # Wait for all the tasks to complete
    icebridge_common.waitForTaskCompletionOrKeypress(taskHandles, options.interactive, 
                                                     quitKey='q', sleepTime=sleepTime)
    
    # Either all the tasks are finished or the user requested a cancel.
    # Clean up the processing pool
    icebridge_common.stopTaskPool(pool)


# Run main function if file used from shell
if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))



