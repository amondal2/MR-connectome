#!/usr/bin/python

'''
@author : Disa Mhembere
@organization: Johns Hopkins University
@contact: disa@jhu.edu

@summary: Module to hold the views of a Django one-click MR-connectome pipeline
'''

import os, sys, re
os.environ['MPLCONFIGDIR'] = '/tmp/'
#import matplotlib
#matplotlib.use( 'Agg' )

import zipfile
import tempfile

from django.shortcuts import render_to_response
from django.template import RequestContext
from django.http import HttpResponseRedirect
from django.http import HttpResponse
from django.http import HttpResponseBadRequest

from django.core.files import File        # For programmatic file upload

# Model & Form imports
from models import BuildGraphModel
from models import OwnedProjects
from forms import DownloadForm
from forms import GraphUploadForm
from forms import ConvertForm
from forms import BuildGraphForm
from forms import PasswordResetForm

import mrpaths

''' Data Processing imports'''
from mrcap import gengraph as gengraph

import filesorter as filesorter
import zipper as zipper
import createDirStruct as createDirStruct
import computation.utils.convertTo as convertTo

from django.core.servers.basehttp import FileWrapper

import subprocess
from django.core.urlresolvers import get_script_prefix
from django.conf import settings

from time import strftime, localtime
from django.core.files.storage import default_storage
from django.core.files.base import ContentFile
from django.shortcuts import redirect

####################
## Graph Analysis ##
####################
import computation.composite.invariants as cci
import scipy.io as sio

# Registration
from django.contrib.auth import authenticate, login, logout

# Helpers
from util import *

''' Base url just redirects to welcome '''
def default(request):
  return redirect(get_script_prefix()+'welcome', {"user":request.user})

''' Little welcome message '''
def welcome(request):
  return render_to_response('welcome.html', {"user":request.user},
                            context_instance=RequestContext(request))


# Login decorator
#from django.contrib.auth.decorators import login_required
#@login_required(redirect_field_name='my_redirect_field')
#@login_required # OR EASIER
def buildGraph(request):

  error_msg = ''

  if request.method == 'POST':
    form = BuildGraphForm(request.POST, request.FILES) # instantiating form
    if form.is_valid():

      # Acquire proj names
      userDefProjectName = form.cleaned_data['UserDefprojectName']
      site = form.cleaned_data['site']
      subject = form.cleaned_data['subject']
      session = form.cleaned_data['session']
      scanId = form.cleaned_data['scanId']

      # Private project error checking
      if (form.cleaned_data['Project_Type'] == 'private'):
        if not request.user.is_authenticated():
          error_msg = "You must be logged in to make/alter a private project! Please Login or make/alter a public project."

        '''
        # Untested TODO: Add join to ensure it a private project
        elif BuildGraphModel.objects.filter(owner=request.user, project_name=userDefProjectName, \
                                    site=site, subject=subject, session=session, scanId=scanId).exists():

           error_msg = "The scanID you requested to create already exists in this project path. Please change any of the form values."
        '''
      # TODO DM: Many unaccounted for scenarios here!

      if error_msg:
        return render_to_response(
          'buildgraph.html',
          {'buildGraphform': form, 'error_msg': error_msg},
          context_instance=RequestContext(request)
          )

      print "Uploading files..."

      # If a user is logged in associate the project with thier directory
      if form.cleaned_data['Project_Type'] == 'private':
        userDefProjectName = os.path.join(request.user.username, userDefProjectName)
      else:
        userDefProjectName = os.path.join('public', userDefProjectName)

      # Adapt project name if necesary on disk
      userDefProjectName = adaptProjNameIfReq(os.path.join(settings.MEDIA_ROOT, userDefProjectName)) # Fully qualify AND handle identical projects

      request.session['usrDefProjDir'] = os.path.join(userDefProjectName, site, subject, session, scanId)
      request.session['scanId'] = scanId

      ''' Define data directory paths '''
      request.session['derivatives'], request.session['rawdata'], request.session['graphs'],\
          request.session['graphInvariants'],request.session['images']= defDataDirs(request.session['usrDefProjDir'])

      # Create a model object to save data to DB

      grModObj = BuildGraphModel(project_name = form.cleaned_data['UserDefprojectName'])
      grModObj.location = request.session['usrDefProjDir'] # Where the particular scan location is

      grModObj.site = form.cleaned_data['site']# set the site
      grModObj.subject = form.cleaned_data['subject']# set the subject
      grModObj.session = form.cleaned_data['session']# set the session
      grModObj.scanId = form.cleaned_data['scanId']# set the scanId

      if request.user.is_authenticated():
        grModObj.owner = request.user # Who created the project

      request.session['invariants'] = form.cleaned_data['Select_Invariants_you_want_computed']
      request.session['graphsize'] = form.cleaned_data['Select_graph_size']

      ''' Acquire fileNames '''
      fiber_fn = form.cleaned_data['fiber_file'].name # get the name of the file input by user
      roi_raw_fn = form.cleaned_data['roi_raw_file'].name
      roi_xml_fn = form.cleaned_data['roi_xml_file'].name

      print "Uploading files..."


      ''' Save files in appropriate location '''
      saveFileToDisk(form.cleaned_data['fiber_file'], os.path.join(request.session['derivatives'], fiber_fn))
      saveFileToDisk(form.cleaned_data['roi_raw_file'], os.path.join(request.session['derivatives'], roi_raw_fn))
      saveFileToDisk(form.cleaned_data['roi_xml_file'], os.path.join(request.session['derivatives'], roi_xml_fn))

      grModObj.save() # Save project data to DB after file upload

      # add entry to owned project
      if request.user.is_authenticated():
        ownedProjModObj = OwnedProjects(project_name=grModObj.project_name, \
                                        owner=grModObj.owner, is_private=form.cleaned_data['Project_Type'] == 'private')
        ownedProjModObj.save()

      print '\nSaving all files complete...'

      ''' Make appropriate dirs if they dont already exist '''
      createDirStruct.createDirStruct([request.session['derivatives'], request.session['rawdata'],\
          request.session['graphs'], request.session['graphInvariants'], request.session['images']])

      # Redirect to Processing page
      return HttpResponseRedirect(get_script_prefix()+'processinput')
  else:
    form = BuildGraphForm() # An empty, unbound form

  # Render the form
  return render_to_response(
      'buildgraph.html',
      {'buildGraphform': form},
      context_instance=RequestContext(request) # Some failure to input data & returns a key signaling what is requested
  )

''' Successful completion of task'''
def success(request):
  return render_to_response('success.html')

def processInputData(request):
  '''
  Extract File name & determine what file corresponds to what for gengraph
  @param request: the request object
  '''
  filesInUploadDir = os.listdir(request.session['derivatives'])

  roi_xml_fn, fiber_fn, roi_raw_fn = filesorter.checkFileExtGengraph(filesInUploadDir) # Check & sort files

  for fileName in [roi_xml_fn, fiber_fn, roi_raw_fn]:
    if fileName == "": # Means a file is missing from i/p
      return render_to_response('pipelineUpload.html', context_instance=RequestContext(request)) # Missing file for processing Gengraph

  baseName = fiber_fn[:-9] # MAY HAVE TO CHANGE

  ''' Fully qualify file names '''
  fiber_fn = os.path.join(request.session['derivatives'], fiber_fn)
  roi_raw_fn = os.path.join(request.session['derivatives'], roi_raw_fn)
  roi_xml_fn = os.path.join(request.session['derivatives'], roi_xml_fn)

  request.session['smGrfn'], request.session['bgGrfn'], request.session['lccfn']\
    ,request.session['SVDfn'] = processData(fiber_fn, roi_xml_fn, roi_raw_fn, \
                                request.session['graphs'], request.session['graphInvariants'],\
                                request.session['graphsize'], True)

  # Run ivariants here
  if len(request.session['invariants']) > 0:
    print "Computing invariants"
    if (request.session['graphsize'] == 'big'):
      graph_fn = request.session['bgGrfn']
      lcc_fn = request.session['lccfn']

    elif (request.session['graphsize'] == 'small'):
      graph_fn = request.session['smGrfn']
      lcc_fn = None

    request.session['invariant_fns'] = runInvariants(request.session['invariants'],\
                                        graph_fn, request.session['graphInvariants'],\
                                        lcc_fn, request.session['graphsize'])

  return HttpResponseRedirect(get_script_prefix()+'confirmdownload')

def confirmDownload(request):

  if request.method == 'POST':
    form = DownloadForm(request.POST) # instantiating form
    if form.is_valid():
      invConvertToFormats = form.cleaned_data['Select_Invariant_conversion_format'] # Which form to convert to
      grConvertToFormats = form.cleaned_data['Select_Graph_conversion_format']
      dataReturn = form.cleaned_data['Select_output_type']

      for fileFormat in invConvertToFormats:
        if fileFormat == '.mat':
          convertTo.convertLCCNpyToMat(request.session['lccfn'])
          convertTo.convertSVDNpyToMat(request.session['SVDfn'])

        # Conversion of all files
        for inv in request.session['invariant_fns'].keys():
          if isinstance(request.session['invariant_fns'][inv], list): # Case of eigs
            for fn in request.session['invariant_fns'][inv]:
              convertTo.convertAndSave(fn, fileFormat, getDirFromFilename(fn), inv)
              # TODO replace getDirFromFilename with os.path.dirname
          else: # case of all other invariants
            convertTo.convertAndSave(request.session['invariant_fns'][inv], fileFormat, \
                                getDirFromFilename(request.session['invariant_fns'][inv]) , inv)
            # TODO replace getDirFromFilename with os.path.dirname

      for fileFormat in grConvertToFormats:
        if request.session['graphsize'] == 'big':
          convertTo.convertGraph(request.session['bgGrfn'], fileFormat)
        elif request.session['graphsize'] == 'small':
          convertTo.convertGraph(request.session['smGrfn'], fileFormat)

      if dataReturn == 'vd': # View data directory
        dataUrlTail = request.session['usrDefProjDir']

        # baseurl = request.META['HTTP_HOST']
        # host = request.META['wsgi.url_scheme']
        # rooturl = host + '://' + baseurl # Originally was: 'http://mrbrain.cs.jhu.edu' # Done for http & https

        return HttpResponseRedirect('http://mrbrain.cs.jhu.edu' + dataUrlTail.replace(' ','%20'))

      elif dataReturn == 'dz': #Download all as zip
        return HttpResponseRedirect(get_script_prefix()+'zipoutput')

  else:
    form = DownloadForm()

  return render_to_response('confirmDownload.html',{'downloadForm': form},\
                  context_instance=RequestContext(request))

def zipProcessedData(request):
  '''
  Compress data products to single zip for upload
  @param request: the request object
  '''
  print '\nBeginning file compression...'
  # Take dir with multiple scans, compress it & send it off

  ''' Zip it '''
  #temp = zipper.zipFilesFromFolders(dirName = request.session['usrDefProjDir'])
  temp = zipper.zipper(request.session['usrDefProjDir'], zip_file = request.session['usrDefProjDir'] + '.zip')
  ''' Wrap it '''
  wrapper = FileWrapper(temp)
  response = HttpResponse(wrapper, content_type='application/zip')
  response['Content-Disposition'] = ('attachment; filename='+ request.session['scanId'] +'.zip')
  response['Content-Length'] = temp.tell()
  temp.seek(0)

  # request.session.clear() # Very Important
  ''' Send it '''
  return response

def upload(request, webargs=None):
  """
  Programmatic interface for uploading data
  @param request: the request object

  @param webargs: POST data with userDefProjectName, site, subject, session, scanId, graphsize, [list of invariants to compute] info
  """
  # request.session.clear()

  if (webargs and request.method == 'POST'):
    # Check for malformatted input
    webargs = webargs[1:] if webargs.startswith('/') else webargs
    webargs = webargs[:-1] if webargs.endswith('/') else webargs

    if len(webargs.split('/')) == 7:
      [userDefProjectName, site, subject, session, scanId, graphsize, request.session['invariants'] ] = webargs.split('/')
      request.session['invariants'] = request.session['invariants'].split(',')
    elif len(webargs.split('/')) == 6:
      [userDefProjectName, site, subject, session, scanId, graphsize] = webargs.split('/')

    userDefProjectDir = adaptProjNameIfReq(os.path.join(settings.MEDIA_ROOT, 'public', userDefProjectName, site, subject, session, scanId))

    ''' Define data directory paths '''
    derivatives, rawdata,  graphs, request.session['graphInvariants'], images = defDataDirs(userDefProjectDir)

    ''' Make appropriate dirs if they dont already exist '''
    createDirStruct.createDirStruct([derivatives, rawdata, graphs, request.session['graphInvariants'], images])
    print 'Directory structure created...'

    uploadFiles =  writeBodyToDisk(request.body, derivatives)

    # Check which file is which
    roi_xml_fn, fiber_fn, roi_raw_fn = filesorter.checkFileExtGengraph(uploadFiles) # Check & sort files

    ''' Data Processing '''
    if (re.match(re.compile('(b|big)', re.IGNORECASE), graphsize)):
      request.session['graphsize'] = 'big'
      request.session['smGrfn'], request.session['bgGrfn'], lccfn, SVDfn \
        = processData(fiber_fn, roi_xml_fn, roi_raw_fn,graphs,\
                      request.session['graphInvariants'],\
                      request.session['graphsize'],True)

    elif(re.match(re.compile('(s|small)', re.IGNORECASE), graphsize)):
      request.session['graphsize'] = 'small'
      request.session['smGrfn'], request.session['bgGrfn'], lccfn, SVDfn \
        = processData(fiber_fn, roi_xml_fn, roi_raw_fn,graphs, request.session['graphInvariants'], request.session['graphsize'],True)

    else:
      return django.http.HttpResponseBadRequest ("Missing graph size. Specify big or small")

    # Run invariants
    if len(request.session['invariants']) > 0:
      print "Computing invariants"
      if (request.session['graphsize'] == 'big'):
        graph_fn = request.session['bgGrfn']
        lcc_fn = request.session['lccfn']

      elif (request.session['graphsize'] == 'small'):
        graph_fn = request.session['smGrfn']
        lcc_fn = None

      invariant_fns = runInvariants(request.session['invariants'],\
                                          graph_fn, request.session['graphInvariants'],\
                                          lcc_fn, request.session['graphsize'])

    #ret = rzfile.printdir()
    #ret = rzfile.testzip()
    #ret = rzfile.namelist()

    # request.session.clear()

    dwnldLoc = "http://mrbrain.cs.jhu.edu" + userDefProjectDir.replace(' ','%20')
    return HttpResponse ( "Files available for download at " + dwnldLoc) # change to render of a page with a link to data result

  elif(not webargs):
    # request.session.clear()
    return django.http.HttpResponseBadRequest ("Expected web arguments to direct project correctly")

  else:
    # request.session.clear()
    return django.http.HttpResponseBadRequest ("Expected POST data, but none given")

################## TO DOs ########################
def download(request, webargs=None):
  # DM: TODO - Allow for data to be downloaded by the directory name/filename/projectName
  pass

#########################################
#	*******************		#
#	   GRAPH LOAD VIEW		#
#########################################
def graphLoadInv(request, webargs=None):
  ''' Form '''
  from glob import glob # Move
  # request.session.clear() # NEW

  if request.method == 'POST' and not webargs:
    form = GraphUploadForm(request.POST, request.FILES) # instantiating form
    if form.is_valid():
      data = form.files['fileObj'] # get data
      request.session['invariants'] = form.cleaned_data['Select_Invariants_you_want_computed']

      request.session['graphsize'] = form.cleaned_data['Select_graph_size']

      dataDir = os.path.join(settings.MEDIA_ROOT, 'tmp', strftime("projectStamp%a%d%b%Y_%H.%M.%S/", localtime()))
      request.session['graphInvariants'] = os.path.join(dataDir, 'graphInvariants')

      makeDirIfNone([dataDir])

      # We got a zip
      if os.path.splitext(data.name)[1] == '.zip':

        writeBodyToDisk(data.read(), dataDir)
        # Get all graphs in the directory
        graphs = glob(os.path.join(dataDir,'*_fiber.mat'))
        graphs.extend(glob(os.path.join(dataDir,'*_bggr.mat')))
        graphs.extend(glob(os.path.join(dataDir,'*_smgr.mat')))

      else: # View only accepts .mat & zip as regulated by template
        graphs = [os.path.join(dataDir, data.name)]
        saveFileToDisk(data, graphs[0])

      for graph_fn in graphs:
        if request.session['graphsize'] == 'big':
          request.session['bgGrfn'] = graph_fn
          lcc_fn = graph_fn.split('_')[0] + '_concomp.mat'

        elif request.session['graphsize'] == 'small':
          graph_fn = request.session['smGrfn'] = graph_fn
          lcc_fn = None

          runInvariants(request.session['invariants'], graph_fn,
                        request.session['graphInvariants'], lcc_fn,
                        request.session['graphsize'])
          print 'Invariants for annoymous project %s complete...' % graph_fn


      return HttpResponseRedirect("http://mrbrain.cs.jhu.edu"+ dataDir.replace(' ','%20')) # All spaces are replaced with %20 for urls

  elif request.method == 'POST' and webargs:
    if (re.match(re.compile('(b|big)', re.IGNORECASE), webargs.split('/')[0])):
      request.session['graphsize'] = 'big'
    elif (re.match(re.compile('(s|small)', re.IGNORECASE), webargs.split('/')[0])):
       request.session['graphsize'] = 'small'
    else:
      return django.http.HttpResponseBadRequest("The graph size is required as a web argument")

    dataDir = os.path.join(settings.MEDIA_ROOT, 'tmp', strftime("projectStamp%a%d%b%Y_%H.%M.%S/", localtime()))
    makeDirIfNone([dataDir])

    uploadedZip = writeBodyToDisk(request.body, dataDir)[0]

    zipper.unzip(uploadedZip, dataDir) # Unzip the zip
    os.remove(uploadedZip) # Delete the zip)

    request.session['invariants'] = webargs.split('/')[1].split(',')

    graphs = glob(os.path.join(dataDir,'*_fiber.mat'))
    graphs.extend(glob(os.path.join(dataDir,'*_bggr.mat')))
    graphs.extend(glob(os.path.join(dataDir,'*_smgr.mat')))

    request.session['graphInvariants'] = os.path.join(dataDir, 'graphInvariants')

    for graph_fn in graphs:
      if request.session['graphsize'] == 'big':
        request.session['bgGrfn'] = graph_fn
        lcc_fn = graph_fn.split('_')[0] + '_concomp.mat'

      elif request.session['graphsize'] == 'small':
        request.session['smGrfn'] = graph_fn
        lcc_fn = None

      runInvariants(request.session['invariants'], graph_fn,
                        request.session['graphInvariants'], lcc_fn,
                        request.session['graphsize'])
      print 'Invariants for annoymous project %s complete...' % graph_fn

    # request.session.clear()
    dwnldLoc = "http://mrbrain.cs.jhu.edu"+ dataDir.replace(' ','%20')
    return HttpResponse("View Data at: " + dwnldLoc)

  else:
    form = GraphUploadForm() # An empty, unbound form

  # Render the form
  return render_to_response(
      'graphupload.html',
      {'graphUploadForm': form},
      context_instance=RequestContext(request) # Some failure to input data & returns a key signaling what is requested
  )

#########################################
#	*******************		#
#	  CONVERT GRAPH FORMAT		#
#########################################

def convert(request, webargs=None):
  ''' Form '''
  # request.session.clear() # NEW

  if (request.method == 'POST' and not webargs):
    form = ConvertForm(request.POST, request.FILES) # instantiating form
    if form.is_valid():

      baseDir = os.path.join(settings.MEDIA_ROOT, 'tmp', strftime('formUpload%a%d%b%Y_%H.%M.%S/', localtime()))
      saveDir = os.path.join(baseDir,'upload') # Save location of original uploads
      convertFileSaveLoc = os.path.join(baseDir,'converted') # Save location of converted data

      if not (os.path.exists(convertFileSaveLoc)):
        os.makedirs(convertFileSaveLoc)

      savedFile = os.path.join(saveDir, request.FILES['fileObj'].name)

      saveFileToDisk(request.FILES['fileObj'], savedFile)

      # If zip is uploaded
      if os.path.splitext(request.FILES['fileObj'].name)[1].strip() == '.zip':
        uploadedFiles = zipper.unzip(savedFile, saveDir)
        # Delete zip
        os.remove(savedFile)
      else:
        uploadedFiles = [savedFile]

      isCorrectFileFormat, isCorrectFileType = convertFiles(uploadedFiles, form.cleaned_data['Select_file_type'], \
                                                      form.cleaned_data['Select_conversion_format'], convertFileSaveLoc)

      if not (isCorrectFileFormat):
        err_msg = "You did not upload any files with the correct extension for conversion!"
        return render_to_response(
        'convertupload.html',
        {'convertForm': form, 'err_msg': err_msg},
        context_instance=RequestContext(request))


      baseurl = request.META['HTTP_HOST']
      host = request.META['wsgi.url_scheme']
      rooturl = host + '://' + baseurl # Originally was: 'http://mrbrain.cs.jhu.edu' # Done for http & https

      dwnldLoc = rooturl + convertFileSaveLoc.replace(' ','%20') # TODO: Verify this works
      return HttpResponseRedirect(dwnldLoc)

  # Programmtic API
  elif(request.method == 'POST' and webargs):
    # webargs is {fileType}/{toFormat}
    fileType = webargs.split('/')[0] # E.g 'cc', 'deg', 'triangle'
    toFormat =  (webargs.split('/')[1]).split(',')   # E.g 'mat', 'npy' or 'mat,csv'

    toFormat = list(set(toFormat)) # Eliminate duplicates if any exist

    # Make sure filetype is valid before doing any work
    if (fileType not in settings.VALID_FILE_TYPES.keys() and fileType not in settings.VALID_FILE_TYPES.values()):
      return HttpResponse('Invalid conversion type. Make sure toFormat is a valid type')

    # In case to format does not start with a '.'. Add if not
    for idx in range (len(toFormat)):
      if not toFormat[idx].startswith('.'):
        toFormat[idx] = '.'+toFormat[idx]

    baseDir = os.path.join(settings.MEDIA_ROOT, 'tmp', strftime('progUpload%a%d%b%Y_%H.%M.%S/', localtime()))
    saveDir = os.path.join(baseDir,'upload') # Save location of original uploads
    convertFileSaveLoc = os.path.join(baseDir,'converted') # Save location of converted data

    if not os.path.exists(saveDir):
      os.makedirs(saveDir)

    if not os.path.exists(convertFileSaveLoc):
      os.makedirs(convertFileSaveLoc)

    uploadedFiles = writeBodyToDisk(request.body, saveDir)

    isCorrectFileFormat, isCorrectFileType = convertFiles(uploadedFiles, fileType, toFormat, convertFileSaveLoc)

    if not (isCorrectFileType):
      # request.session.clear()
      return HttpResponse("[ERROR]: You did not enter a valid FileType.")
    if not (isCorrectFileFormat):
      # request.session.clear()
      return HttpResponse("[ERROR]: You do not have any files with the correct extension for conversion")

    dwnldLoc = "http://mrbrain.cs.jhu.edu"+ convertFileSaveLoc.replace(' ','%20')
    # request.session.clear()
    return HttpResponse ( "Converted files available for download at " + dwnldLoc + " . The directory " +
            "may be empty if you try to convert to the same format the file is already in.") # change to render of a page with a link to data result

  else:
    form = ConvertForm() # An empty, unbound form

  # Render the form
  return render_to_response(
      'convertupload.html',
      {'convertForm': form},
      context_instance=RequestContext(request))

#########################################
#	*******************		#
#	   PROCESS DATA			#
#########################################

def processData(fiber_fn, roi_xml_fn, roi_raw_fn,graphs, graphInvariants, graphsize, run = False):
  '''
  Run graph building and other related scripts
  @param fiber_fn: fiber tract file
  @param roi_xml_fn: region of interest xml file
  @param roi_raw_fn: region of interest raw file

  @param graphs: Dir where biggraphs & smallgraphs are saved
  @param graphInvariants:  Dir where graph invariants are saved
  @param graphsize: the size of the graph 'big' or 'small'
  @param run: Whether or not to run processor intensive jobs. Default is - false so nothing is actually run
  '''
  if (run):
    import mrcap.svd as svd
    import mrcap.lcc as lcc
    print "Imported svd and lcc modules..."

  baseName = getFiberID(fiber_fn) #VERY TEMPORARY

  smGrfn = os.path.join(graphs, (baseName +'smgr.mat'))
  bgGrfn = os.path.join(graphs, (baseName +'bggr.mat'))

  if (run):
    if (graphsize == 'small'):
      ''' Run gengraph SMALL & save output '''
      print("Running Small gengraph....")
      gengraph.genGraph(fiber_fn, smGrfn, roi_xml_fn, roi_raw_fn, bigGraph=False)

    elif(graphsize == 'big'):
      ''' Run gengrah BIG & save output '''
      print("\nRunning Big gengraph....")
      gengraph.genGraph(fiber_fn, bgGrfn, roi_xml_fn, roi_raw_fn, bigGraph=True)
    else:
      print '[ERROR]: Graphsize Unkwown' # should never happen

  ''' Run LCC '''
  lccfn = os.path.join(graphInvariants,"LCC", (baseName + 'concomp.npy'))

  if (run):
    '''Should be big but we'll do small for now'''
    if (graphsize == 'big'):
      print "Running biggraph Largest connected component..."
      lcc.process_single_brain(bgGrfn, lccfn)
    if (graphsize == 'small'):
      print "Running smallgraph Largest connected component..."
      lcc.process_single_brain(smGrfn, lccfn)

  ''' Run Embed - SVD '''
  SVDfn = os.path.join(graphInvariants,"SVD" ,(baseName + 'embed.npy'))

  print("Running SVD....")
  if (run):
    if (graphsize == 'big'):
      print "Running SVD on biggraph"
      svd.embed_graph(lccfn, bgGrfn, SVDfn)
    if (graphsize == 'small'):
      print "Running SVD on smallgraph"
      svd.embed_graph(lccfn, smGrfn, SVDfn)

  print "Completed generating - graph, lcc & svd"
  return [ smGrfn, bgGrfn, lccfn, SVDfn ]

###############################################################################
#                          NEW INVARIANTS                                     #
###############################################################################

def runInvariants(inv_list, graph_fn, save_dir, lcc_fn, graphsize):
  '''

  '''

  inv_dict = {'graph_fn':graph_fn, 'save_dir':save_dir, \
              'lcc_fn':lcc_fn,'graphsize':graphsize}
  for inv in inv_list:
    inv_dict[inv] = True
  inv_dict = cci.compute(inv_dict)

  return_dict = dict()

  if inv_dict['ss1_fn']:
    return_dict['ss1'] = inv_dict['ss1_fn']
  if inv_dict['tri_fn']:
    return_dict['tri'] = inv_dict['tri_fn']
  if inv_dict['deg_fn']:
    return_dict['deg'] = inv_dict['deg_fn']
  if inv_dict['ss2_fn']:
    return_dict['ss2'] = inv_dict['ss2_fn']
  if inv_dict['apl_fn']:
    return_dict['apl'] = inv_dict['apl_fn']
  if inv_dict['gdia_fn']:
    return_dict['gdia'] = inv_dict['gdia_fn']
  if inv_dict['cc_fn']:
    return_dict['cc'] = inv_dict['cc_fn']
  if inv_dict['mad_fn']:
    return_dict['mad'] = inv_dict['mad_fn']
  if inv_dict['eigvl_fn']:
    return_dict['eig'] = [inv_dict['eigvect_fn'], inv_dict['eigvl_fn']] # Note this

  return return_dict

