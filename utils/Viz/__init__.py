__author__    = 'Mike McCann'
__copyright__ = '2012'
__license__   = 'GPL v3'
__contact__   = 'mccann at mbari.org'

__doc__ = '''

Module with various functions to supprt data visualization.  These can be quite verbose
with all of the Matplotlib customization required for nice looking graphics.

@undocumented: __doc__ parser
@status: production
@license: GPL
'''

import os
import tempfile
# Setup Matplotlib for running on the server
os.environ['MPLCONFIGDIR'] = tempfile.mkdtemp()
import matplotlib as mpl
mpl.use('Agg')               # Force matplotlib to not use any Xwindows backend
import matplotlib.pyplot as plt
from matplotlib.mlab import griddata
from matplotlib import figure
from matplotlib.backends.backend_agg import FigureCanvasAgg as FigureCanvas
from pylab import polyval
from django.conf import settings
from django.db.models.query import RawQuerySet
from django.db import connections, DatabaseError, transaction
from datetime import datetime
from KML import readCLT
from stoqs import models
from utils.utils import postgresifySQL, pearsonr, round_to_n, EPOCH_STRING
from utils.MPQuery import MPQuerySet
from utils.geo import GPS
from loaders.SampleLoaders import SAMPLED, NETTOW, VERTICALNETTOW
from loaders import MEASUREDINSITU
import seawater.csiro as sw
import numpy as np
from numpy import polyfit
from itertools import izip
import logging
import string
import random
import time
import re

logger = logging.getLogger(__name__)

def _getCoordUnits(name):
    '''
    Assign units given a standard coordinate name
    '''
    if name == 'longitude':
        units = 'degrees_east'
    elif name == 'latitude':
        units = 'degrees_north'
    elif name == 'depth':
        units = 'm'
    elif name == 'time':
        units = 'days since %s' % EPOCH_STRING
    else:
        units = ''

    return units

def makeColorBar(request, colorbarPngFileFullPath, parm_info, colormap, orientation='horizontal'):
    '''
    Utility function used by classes in this module to create a colorbar image accessible at @colorbarPngFileFullPath.
    The @requst object is needed to use the database alias.
    @parm_info is a 3 element list/tuple: (parameterId, minValue, maxValue).
    @colormap is a color the color lookup table.
    If @orientation is 'vertical' create a vertically oriented image, otherwise horizontal.
    '''

    if orientation == 'horizontal':
        cb_fig = plt.figure(figsize=(5, 0.8))
        cb_ax = cb_fig.add_axes([0.1, 0.8, 0.8, 0.2])
        norm = mpl.colors.Normalize(vmin=parm_info[1], vmax=parm_info[2], clip=False)
        ticks=round_to_n(list(np.linspace(parm_info[1], parm_info[2], num=4)), 4)
        cb = mpl.colorbar.ColorbarBase( cb_ax, cmap=colormap,
                                        norm=norm,
                                        ticks=ticks,
                                        orientation='horizontal')
        cb.ax.set_xticklabels(ticks)
        try:
            cp = models.Parameter.objects.using(request.META['dbAlias']).get(id=int(parm_info[0]))
        except ValueError:
            # Likely a coordinate variable
            cp = models.Parameter
            cp.name = parm_info[0]
            cp.standard_name = parm_info[0]
            cp.units = _getCoordUnits(parm_info[0])

        cb.set_label('%s (%s)' % (cp.name, cp.units))
        cb_fig.savefig(colorbarPngFileFullPath, dpi=120, transparent=True)
        plt.close()

    elif orientation == 'vertical':
        cb_fig = plt.figure(figsize=(0.6, 4))
        cb_ax = cb_fig.add_axes([0.1, 0.1, 0.15, 0.8])
        norm = mpl.colors.Normalize(vmin=parm_info[1], vmax=parm_info[2], clip=False)
        cb = mpl.colorbar.ColorbarBase( cb_ax, cmap=colormap,
                                        norm=norm,
                                        ticks=list(np.linspace(parm_info[1], parm_info[2], num=4)),
                                        orientation='vertical')
        cb.ax.set_yticklabels([str(parm_info[1]), str(parm_info[2])])
        logger.debug('Getting units for parm_info[0] = %s', parm_info[0])
        try:
            cp = models.Parameter.objects.using(request.META['dbAlias']).get(id=int(parm_info[0]))
        except ValueError:
            # Likely a coordinate variable
            cp = models.Parameter
            cp.name = parm_info[0]
            cp.standard_name = parm_info[0]
            cp.units = _getCoordUnits(parm_info[0])

        cb.set_label('%s (%s)' % (cp.name, cp.units), fontsize=10)
        for label in cb.ax.get_yticklabels():
            label.set_fontsize(10)
            label.set_rotation('vertical')
        cb_fig.savefig(colorbarPngFileFullPath, dpi=120, transparent=True)
        plt.close()

    else:
        raise Exception('orientation must be either horizontal or vertical')


class MeasuredParameter(object):
    '''
    Use matploptib to create nice looking contour plots
    '''
    logger = logging.getLogger(__name__)
    def __init__(self, kwargs, request, qs, qs_mp, parameterMinMax, sampleQS, platformName, parameterID=None, parameterGroups=[MEASUREDINSITU]):
        '''
        Save parameters that can be used by the different product generation methods here
        parameterMinMax is like: (pName, pMin, pMax)
        '''
        self.kwargs = kwargs
        self.request = request
        self.qs = qs
        self.qs_mp = qs_mp                      # Calling routine passes the _no_order version of the QuerySet
        self.parameterMinMax = parameterMinMax
        self.sampleQS = sampleQS
        self.platformName = platformName
        self.parameterID = parameterID
        self.parameterGroups = parameterGroups

        self.scale_factor = None
        self.clt = readCLT(os.path.join(settings.STATIC_ROOT, 'colormaps', 'jetplus.txt'))
        self.cm_jetplus = mpl.colors.ListedColormap(np.array(self.clt))
        # - Use a new imageID for each new image
        self.imageID = ''.join(random.choice(string.ascii_uppercase + string.digits) for x in range(10))
        if self.parameterID:
            self.colorbarPngFile = str(self.parameterID) + '_' + self.platformName + '_colorbar_' + self.imageID + '.png'
        else:
            self.colorbarPngFile = self.kwargs['measuredparametersgroup'][0] + '_' + self.platformName + '_colorbar_' + self.imageID + '.png'

        self.colorbarPngFileFullPath = os.path.join(settings.MEDIA_ROOT, 'sections', self.colorbarPngFile)
        self.x = []
        self.y = []
        self.z = []
        self.lat = []
        self.lon = []
        self.depth = []
        self.value = []

        self.xspan = []
        self.yspan = []
        self.zspan = []
        self.latspan = []
        self.lonspan = []
        self.depthspan = []

    def _fillXYZ(self, mp, sampled=False, spanned=False, activitytype=None):
        '''
        Fill up the x, y, and z member lists for measured (default) or sampled data values. 
        If spanned is True then fill xspan, yspan, and zspan member lists with NetTow like data.
        '''
        if sampled:
            if self.scale_factor:
                self.x.append(time.mktime(mp['sample__instantpoint__timevalue'].timetuple()) / self.scale_factor)
            else:
                self.x.append(time.mktime(mp['sample__instantpoint__timevalue'].timetuple()))
            self.y.append(mp['sample__depth'])
            self.depth_by_act.setdefault(mp['sample__instantpoint__activity__name'], []).append(float(mp['sample__depth']))
            self.z.append(mp['datavalue'])
            self.value_by_act.setdefault(mp['sample__instantpoint__activity__name'], []).append(float(mp['datavalue']))

            if 'sample__geom' in mp.keys():
                self.lon.append(mp['sample__geom'].x)
                self.lon_by_act.setdefault(mp['sample__instantpoint__activity__name'], []).append(mp['sample__geom'].x)
                self.lat.append(mp['sample__geom'].y)
                self.lat_by_act.setdefault(mp['sample__instantpoint__activity__name'], []).append(mp['sample__geom'].y)

            if spanned and activitytype == VERTICALNETTOW:
                # Save a (start, end) tuple for each coordinate/value, VERTICALNETTOWs start at maxdepth
                if self.scale_factor:
                    self.xspan.append(
                            (time.mktime(mp['sample__instantpoint__activity__startdate'].timetuple()) / self.scale_factor,
                             time.mktime(mp['sample__instantpoint__activity__enddate'].timetuple()) / self.scale_factor)
                                     )
                else:
                    self.xspan.append(
                            (time.mktime(mp['sample__instantpoint__activity__startdate'].timetuple()),
                             time.mktime(mp['sample__instantpoint__activity__enddate'].timetuple()))
                                     )
                self.yspan.append(
                        (mp['sample__instantpoint__activity__maxdepth'],
                         mp['sample__instantpoint__activity__mindepth'])
                                 )
                self.depth_by_act_span.setdefault(mp['sample__instantpoint__activity__name'], []).append(
                        (mp['sample__instantpoint__activity__maxdepth'],
                         mp['sample__instantpoint__activity__mindepth'])
                                 )
                self.zspan.append(float(mp['datavalue']))
                self.value_by_act_span.setdefault(mp['sample__instantpoint__activity__name'], []).append(float(mp['datavalue']))

                if 'sample__geom' in mp.keys():
                    # Implemented for VERTICALNETTOW data where start and end geom are identical
                    self.lonspan.append((mp['sample__geom'].x, mp['sample__geom'].x))
                    self.lon_by_act_span.setdefault(mp['sample__instantpoint__activity__name'], []).append(
                            (mp['sample__geom'].x, mp['sample__geom'].x))
                    self.latspan.append((mp['sample__geom'].y, mp['sample__geom'].y))
                    self.lat_by_act_span.setdefault(mp['sample__instantpoint__activity__name'], []).append(
                            (mp['sample__geom'].y, mp['sample__geom'].y))

            # TODO: Implement for other types of spanned data, e.g. use Activity.maptrack to 
            # get start and end geom for other Horizontal or Oblique NetTows
                
        else:
            if self.scale_factor:
                self.x.append(time.mktime(mp['measurement__instantpoint__timevalue'].timetuple()) / self.scale_factor)
            else:
                self.x.append(time.mktime(mp['measurement__instantpoint__timevalue'].timetuple()))
            self.y.append(mp['measurement__depth'])
            self.depth_by_act.setdefault(mp['measurement__instantpoint__activity__name'], []).append(mp['measurement__depth'])
            self.z.append(mp['datavalue'])
            self.value_by_act.setdefault(mp['measurement__instantpoint__activity__name'], []).append(mp['datavalue'])
        
            if 'measurement__geom' in mp.keys():
                self.lon.append(mp['measurement__geom'].x)
                self.lon_by_act.setdefault(mp['measurement__instantpoint__activity__name'], []).append(mp['measurement__geom'].x)
                self.lat.append(mp['measurement__geom'].y)
                self.lat_by_act.setdefault(mp['measurement__instantpoint__activity__name'], []).append(mp['measurement__geom'].y)

    def loadData(self):
        '''
        Read the data from the database into member variables for use by the methods that output various products
        '''
        self.logger.debug('type(self.qs_mp) = %s', type(self.qs_mp))

        # Save to '_by_act' dictionaries so that X3D can end each IndexedLinestring with a '-1'
        self.depth_by_act = {}
        self.value_by_act = {}
        self.lon_by_act = {}
        self.lat_by_act = {}

        self.depth_by_act_span = {}
        self.value_by_act_span = {}
        self.lon_by_act_span = {}
        self.lat_by_act_span = {}

        MP_MAX_POINTS = 10000          # Set by visually examing high-res Tethys data for what looks good
        stride = int(self.qs_mp.count() / MP_MAX_POINTS)
        if stride < 1:
            stride = 1
        self.strideInfo = ''
        if stride != 1:
            self.strideInfo = 'stride = %d' % stride

        self.logger.debug('self.qs_mp.query = %s', str(self.qs_mp.query))
        if SAMPLED in self.parameterGroups:
            for i,mp in enumerate(self.qs_mp):
                self._fillXYZ(mp, sampled=True)
                if (i % 10) == 0:
                    self.logger.debug('Appended %i samples to self.x, self.y, and self.z', i)

            # Build span data members for VERTICALNETTOW activity types
            # TODO: Implement other types as they are needed
            qs = self.qs_mp.filter(sample__instantpoint__activity__activitytype__name__contains=VERTICALNETTOW)
            for i,mp in enumerate(qs):
                self._fillXYZ(mp, sampled=True, spanned=True, activitytype=VERTICALNETTOW)
                if (i % 10) == 0:
                    self.logger.debug('Appended %i samples to self.xspan, self.yspan, and self.zspan', i)
        else:
            self.logger.debug('Reading data with a stride of %s', stride)
            if self.qs_mp.isRawQuerySet:
                # RawQuerySet does not support normal slicing
                i = 0
                self.logger.debug('Slicing with mod division on a counter...')
                for counter,mp in enumerate(self.qs_mp):
                    if counter % stride == 0:
                        self._fillXYZ(mp)
                        i = i + 1
                        if (i % 1000) == 0:
                            self.logger.debug('Appended %i measurements to self.x, self.y, and self.z', i)
            else:
                self.logger.debug('Slicing Pythonicly...')
                for i,mp in enumerate(self.qs_mp[::stride]):
                    self._fillXYZ(mp)
                    if (i % 1000) == 0:
                        self.logger.debug('Appended %i measurements to self.x, self.y, and self.z', i)

        self.depth = self.y
        self.value = self.z

    def _get_samples_for_markers(self, act_name=None, spanned=False, exclude_act_name=None):
        '''
        Return time, depth, and name of Samples for plotting as symbols.
        Restrict to activitytype__name if act_name is specified.
        '''
        # Add sample locations and names, but not if the underlying data are from the Samples themselves
        xsamp = []
        ysamp = []
        sname = []
        qs = self.sampleQS.values('instantpoint__timevalue', 'instantpoint__activity__name', 'depth', 'name')
        if act_name:
            qs = qs.filter(instantpoint__activity__activitytype__name__contains=act_name)
        else:
            if exclude_act_name:
                qs = qs.exclude(instantpoint__activity__activitytype__name__contains=exclude_act_name)

        for s in qs:
            if self.scale_factor:
                xsamp.append(time.mktime(s['instantpoint__timevalue'].timetuple()) / self.scale_factor)
            else:
                xsamp.append(time.mktime(s['instantpoint__timevalue'].timetuple()))
            ysamp.append(s['depth'])
            if act_name:
                # Convention is to use Activity information for things like NetTows
                sname.append(s['instantpoint__activity__name'])
            else:
                sname.append(s['name'])

        if spanned and act_name == VERTICALNETTOW:
            xsamp = []
            ysamp = []
            sname = []
            # Build tuples of start and end for the samples so that lines may be drawn, maxdepth is first
            qs = qs.values('instantpoint__activity__startdate', 'instantpoint__activity__enddate', 
                           'instantpoint__activity__maxdepth', 'instantpoint__activity__mindepth', 
                           'instantpoint__activity__name', 'name').distinct()
            for s in qs:
                if self.scale_factor:
                    xsamp.append((time.mktime(s['instantpoint__activity__startdate'].timetuple()) / self.scale_factor,
                                  time.mktime(s['instantpoint__activity__enddate'].timetuple()) / self.scale_factor))
                else:
                    xsamp.append((time.mktime(s['instantpoint__activity__startdate'].timetuple()),
                                  time.mktime(s['instantpoint__activity__enddate'].timetuple())))

                ysamp.append((s['instantpoint__activity__maxdepth'], s['instantpoint__activity__mindepth']))
                sname.append(s['instantpoint__activity__name'])

        return xsamp, ysamp, sname

    def _get_color(self, datavalue, cmin, cmax):
        '''
        Return RGB color value for data_value given member's color lookup table and cmin, cmax lookup table limits
        '''
        clt = self.cm_jetplus
        indx = int(round((float(datavalue) - cmin) * ((len(clt.colors) - 1) / float(cmax - cmin))))
        if indx < 0:
            indx=0
        if indx >= len(clt.colors):
            indx = len(clt.colors) - 1
        return clt.colors[indx]


    def renderDatavaluesForFlot(self, tgrid_max=1000, dgrid_max=100, dinc=0.5, nlevels=255, contourFlag=True):
        '''
        Produce a .png image without axes suitable for overlay on a Flot graphic. Return a
        3 tuple of (sectionPngFile, colorbarPngFile, errorMessage)

        # griddata parameter defaults
        tgrid_max = 1000            # Reasonable maximum width for time-depth-flot plot is about 1000 pixels
        dgrid_max = 100             # Height of time-depth-flot plot area is 335 pixels
        dinc = 0.5                  # Average vertical resolution of AUV Dorado
        nlevels = 255               # Number of color filled contour levels
        '''

        # Use session ID so that different users don't stomp on each other with their section plots
        # - This does not work for Firefox which just reads the previous image from its cache
        if self.request.session.has_key('sessionID'):
            sessionID = self.request.session['sessionID']
        else:
            sessionID = ''.join(random.choice(string.ascii_uppercase + string.digits) for x in range(7))
            self.request.session['sessionID'] = sessionID

        if self.parameterID:
            sectionPngFile = str(self.parameterID) + '_' + self.platformName + '_' + self.imageID + '.png'
        else:
            sectionPngFile = self.kwargs['measuredparametersgroup'][0] + '_' + self.platformName + '_' + self.imageID + '.png'

        sectionPngFileFullPath = os.path.join(settings.MEDIA_ROOT, 'sections', sectionPngFile)
        
        # Estimate horizontal (time) grid spacing by number of points in selection, expecting that simplified depth-time
        # query has salient points, typically in the vertices of the yo-yos. 
        # If the time tuple has values then use those, they represent a zoomed in portion of the Temporal-Depth flot plot
        # in the UI.  If they are not specified then use the Flot plot limits specified separately in the flotlimits tuple.
        tmin = None
        tmax = None
        xi = None
        if self.kwargs.has_key('time'):
            if self.kwargs['time'][0] is not None and self.kwargs['time'][1] is not None:
                dstart = datetime.strptime(self.kwargs['time'][0], '%Y-%m-%d %H:%M:%S') 
                dend = datetime.strptime(self.kwargs['time'][1], '%Y-%m-%d %H:%M:%S') 
                tmin = time.mktime(dstart.timetuple())
                tmax = time.mktime(dend.timetuple())

        if not tmin and not tmax:
            if self.kwargs['flotlimits'][0] is not None and self.kwargs['flotlimits'][1] is not None:
                tmin = float(self.kwargs['flotlimits'][0]) / 1000.0
                tmax = float(self.kwargs['flotlimits'][1]) / 1000.0

        if tmin and tmax:
            sdt_count = self.qs.filter(platform__name = self.platformName).values_list('simpledepthtime__depth').count()
            sdt_count = int(sdt_count / 2)                 # 2 points define a line, take half the number of simpledepthtime points
            self.logger.debug('Half of sdt_count from query = %d', sdt_count)
            if sdt_count > tgrid_max:
                sdt_count = tgrid_max

            xi = np.linspace(tmin, tmax, sdt_count)
            ##self.logger.debug('xi = %s', xi)

        # If the depth tuple has values then use those, they represent a zoomed in portion of the Temporal-Depth flot plot
        # in the UI.  If they are not specified then use the Flot plot limits specified separately in the flotlimits tuple.
        dmin = None
        dmax = None
        yi = None
        if self.kwargs.has_key('depth'):
            if self.kwargs['depth'][0] is not None and self.kwargs['depth'][1] is not None:
                dmin = float(self.kwargs['depth'][0])
                dmax = float(self.kwargs['depth'][1])

        if not dmin and not dmax:
            if self.kwargs['flotlimits'][2] is not None and self.kwargs['flotlimits'][3] is not None:
                dmin = float(self.kwargs['flotlimits'][2])
                dmax = float(self.kwargs['flotlimits'][3])

        # Make depth spacing dinc m, limit to time-depth-flot resolution (dgrid_max)
        if dmin is not None and dmax is not None:
            y_count = int((dmax - dmin) / dinc )
            if y_count > dgrid_max:
                y_count = dgrid_max
            yi = np.linspace(dmin, dmax, y_count)
            ##self.logger.debug('yi = %s', yi)


        # Collect the scattered datavalues(time, depth) and grid them
        if xi is not None and yi is not None:
            # Estimate a scale factor to apply to the x values on grid data so that x & y values are visually equal for the flot plot
            # which is assumed to be 3x wider than tall.  Approximate horizontal coverage by Dorado is 1 m/s.
            try:
                self.scale_factor = float(tmax -tmin) / (dmax - dmin) / 3.0
            except ZeroDivisionError, e:
                self.logger.warn(e)
                self.logger.debug('Not setting self.scale_factor.  Scatter plots will still work.')
                contourFlag = False
            else:                
                self.logger.debug('self.scale_factor = %f', self.scale_factor)
                xi = xi / self.scale_factor

            try:
                os.remove(sectionPngFileFullPath)
            except Exception, e:
                self.logger.warn('Could not remove file: %s', e)

            if not self.x and not self.y and not self.z:
                self.loadData()

            self.logger.debug('self.kwargs = %s', self.kwargs)
            if self.kwargs.has_key('parametervalues'):
                if self.kwargs['parametervalues']:
                    contourFlag = False
          
            if self.kwargs.has_key('showdataas'):
                if self.kwargs['showdataas']:
                    if self.kwargs['showdataas'][0] == 'scatter':
                        contourFlag = False
          
            self.logger.debug('Number of x, y, z data values retrieved from database = %d', len(self.z)) 
            if len(self.z) == 0:
                return None, None, 'No data returned from selection'

            if contourFlag:
                try:
                    self.logger.debug('Gridding data with sdt_count = %d, and y_count = %d', sdt_count, y_count)
                    zi = griddata(self.x, self.y, self.z, xi, yi, interp='nn')
                except KeyError, e:
                    self.logger.exception('Got KeyError. Could not grid the data')
                    return None, None, 'Got KeyError. Could not grid the data'
                except Exception, e:
                    self.logger.exception('Could not grid the data')
                    return None, None, 'Could not grid the data'

                self.logger.debug('zi = %s', zi)

            COLORED_DOT_SIZE_THRESHOLD = 5000
            if self.qs_mp.count() > COLORED_DOT_SIZE_THRESHOLD:
                coloredDotSize = 10
            else:
                coloredDotSize = 20

            parm_info = self.parameterMinMax
            try:
                # Make the plot
                # contour the gridded data, plotting dots at the nonuniform data points.
                # See http://scipy.org/Cookbook/Matplotlib/Django
                fig = plt.figure(figsize=(6,3))
                ax = fig.add_axes((0,0,1,1))
                if self.scale_factor:
                    ax.set_xlim(tmin / self.scale_factor, tmax / self.scale_factor)
                else:
                    ax.set_xlim(tmin, tmax)
                ax.set_ylim(dmax, dmin)
                ax.get_xaxis().set_ticks([])
                if contourFlag:
                    ax.contourf(xi, yi, zi, levels=np.linspace(parm_info[1], parm_info[2], nlevels), cmap=self.cm_jetplus, extend='both')
                    ax.scatter(self.x, self.y, marker='.', s=2, c='k', lw = 0)
                else:
                    self.logger.debug('parm_info = %s', parm_info)
                    ax.scatter(self.x, self.y, c=self.z, s=coloredDotSize, cmap=self.cm_jetplus, lw=0, vmin=parm_info[1], vmax=parm_info[2])
                    # Draw any spanned data, e.g. NetTows
                    for xs,ys,z in zip(self.xspan, self.yspan, self.zspan):
                        try:
                            ax.plot(xs, ys, c=self._get_color(z, parm_info[1], parm_info[2]), lw=3)
                        except ZeroDivisionError:
                            # Likely all data is same value and color lookup table can't be computed
                            return None, None, "Can't plot identical data values of %f" % z

                if self.sampleQS and SAMPLED not in self.parameterGroups:
                    # Sample markers for everything but Net Tows
                    xsamp, ysamp, sname = self._get_samples_for_markers(exclude_act_name=NETTOW)
                    ax.scatter(xsamp, np.float64(ysamp), marker='o', c='w', s=15, zorder=10)
                    for x,y,sn in izip(xsamp, ysamp, sname):
                        plt.annotate(sn, xy=(x,y), xytext=(5,-5), textcoords = 'offset points', fontsize=7)

                    # Annotate NetTow Samples at Sample record location - points
                    xsamp, ysamp, sname = self._get_samples_for_markers(act_name=NETTOW)
                    ax.scatter(xsamp, np.float64(ysamp), marker='o', c='w', s=15, zorder=10)
                    for x,y,sn in izip(xsamp, ysamp, sname):
                        plt.annotate(sn, xy=(x,y), xytext=(5,-5), textcoords = 'offset points', fontsize=7)

                    # Sample markers for Vertical Net Tows (put circle at surface) - lines
                    xspan, yspan, sname = self._get_samples_for_markers(act_name=VERTICALNETTOW, spanned=True)
                    for xs,ys in zip(xspan, yspan):
                        ax.plot(xs, ys, c='k', lw=2)
                        ax.scatter([xs[1]], [0], marker='o', c='w', s=15, zorder=10)

                fig.savefig(sectionPngFileFullPath, dpi=120, transparent=True)
                plt.close()
            except Exception,e:
                self.logger.exception('Could not plot the data')
                return None, None, 'Could not plot the data'

            try:
                makeColorBar(self.request, self.colorbarPngFileFullPath, parm_info, self.cm_jetplus)
            except Exception,e:
                self.logger.exception('%s', e)
                return None, None, 'Could not plot the colormap'

            return sectionPngFile, self.colorbarPngFile, self.strideInfo
        else:
            self.logger.warn('xi and yi are None.  tmin, tmax, dmin, dmax = %s, %s, %s, %s, %s, %s ', tmin, tmax, dmin, dmax)
            return None, None, 'Select a time-depth range'

    def dataValuesX3D(self, vert_ex=10.0, geoOrigin=None):
        '''
        Return scatter-like data values as X3D geocoordinates and colors. A bit of a HACK until GeoOrigin is
        implemented in X3DOM: if geoOrigin is specified then points will be GCC (ECEF) coordinates with the 
        geoOrigin subtracted, GD input coordinates assumed.
        '''
        showGeoX3DDataFlag = False
        if self.kwargs.has_key('showgeox3ddata'):
            if self.kwargs['showgeox3ddata']:
                if self.kwargs['showgeox3ddata']:
                    showGeoX3DDataFlag = True

        logger.debug("Building X3D data values with vert_ex = %f", vert_ex)

        x3dResults = {}
        if not showGeoX3DDataFlag:
            return x3dResults

        if not self.lon and not self.lat and not self.depth and not self.value:
            self.logger.debug('Calling self.loadData()...')
            self.loadData()
        try:
            points = ''
            colors = ''
            indices = ''
            index = 0
            gps = GPS()
            for act in self.value_by_act.keys():
                self.logger.debug('Reading data from act = %s', act)
                for lon,lat,depth,value in izip(self.lon_by_act[act], self.lat_by_act[act], self.depth_by_act[act], self.value_by_act[act]):
                    if geoOrigin:
                        logger.warn('geoOrigin flag use is deprecated as X3DOM (post v1.6) now supports its use')
                        depth -= 45     # Temporary adjustment to make BED01 1-June-2013 event appear above terrain 
                        points = points + '%f %f %f ' % gps.lla2gcc((lat, lon, -depth * vert_ex), geoOrigin)
                    else:
                        points = points + '%.5f %.5f %.1f ' % (lat, lon, -depth * vert_ex)

                    try:
                        cindx = int(round((value - float(self.parameterMinMax[1])) * (len(self.clt) - 1) / 
                                        (float(self.parameterMinMax[2]) - float(self.parameterMinMax[1]))))
                    except ValueError as e:
                        # Likely: 'cannot convert float NaN to integer' as happens when rendering something like altitude outside of terrain coverage
                        continue
                    except ZeroDivisionError as e:
                        logger.error("Can't make color lookup table with min and max being the same, self.parameterMinMax = %s", self.parameterMinMax)
                        raise e

                    if cindx < 0:
                        cindx = 0
                    if cindx > len(self.clt) - 1:
                        cindx = len(self.clt) - 1

                    colors = colors + '%.3f %.3f %.3f ' % (self.clt[cindx][0], self.clt[cindx][1], self.clt[cindx][2])
                    indices = indices + '%i ' % index
                    index = index + 1

                # End the IndexedLinestring with -1 so that end point does not connect to the beg point
                indices = indices + '-1 ' 

            # Make pairs of points for spanned NetTow-like data
            for act in self.value_by_act_span.keys():
                self.logger.debug('Reading data from act = %s', act)
                for lons,lats,depths,value in izip(self.lon_by_act_span[act], self.lat_by_act_span[act], self.depth_by_act_span[act], self.value_by_act_span[act]):
                    points = points + '%.5f %.5f %.1f %.5f %.5f %.1f ' % (lats[0], lons[0], -depths[0] * vert_ex, lats[1], lons[1], -depths[1] * vert_ex)
                    try:
                        cindx = int(round((value - float(self.parameterMinMax[1])) * (len(self.clt) - 1) / 
                                        (float(self.parameterMinMax[2]) - float(self.parameterMinMax[1]))))
                    except ValueError as e:
                        # Likely: 'cannot convert float NaN to integer' as happens when rendering something like altitude outside of terrain coverage
                        continue
                    except ZeroDivisionError as e:
                        logger.error("Can't make color lookup table with min and max being the same, self.parameterMinMax = %s", self.parameterMinMax)
                        raise e

                    if cindx < 0:
                        cindx = 0
                    if cindx > len(self.clt) - 1:
                        cindx = len(self.clt) - 1

                    colors = colors + '%.3f %.3f %.3f %.3f %.3f %.3f ' % (self.clt[cindx][0], self.clt[cindx][1], self.clt[cindx][2],
                                                                          self.clt[cindx][0], self.clt[cindx][1], self.clt[cindx][2])
                    indices = indices + '%i %i ' % (index, index + 1)
                    index = index + 2

                # End the IndexedLinestring with -1 so that end point does not connect to the beg point
                indices = indices + '-1 ' 

            try:
                makeColorBar(self.request, self.colorbarPngFileFullPath, self.parameterMinMax, self.cm_jetplus)
            except Exception,e:
                self.logger.exception('Could not plot the colormap')
                x3dResults = 'Could not plot the colormap'
            else:
                x3dResults = {'colors': colors.rstrip(), 'points': points.rstrip(), 'info': '', 'index': indices.rstrip(), 'colorbar': self.colorbarPngFile}

        except Exception as e:
            self.logger.exception('Could not create measuredparameterx3d: %s', e)
            x3dResults = 'Could not create measuredparameterx3d'

        return x3dResults

class PlatformOrientation(object):
    '''
    For Platforms that have Parameters with roll, pitch, and yaw Parameters.
    '''
    logger = logging.getLogger(__name__)

    x3dEulerBaseScene = '''<Transform id="TRANSLATE" DEF="TRANSLATE" scale="1000 1000 1000">
            <Transform id="XROT" DEF="XROT">
                <Transform id="YROT" DEF="YROT">
                    <Transform id="ZROT" DEF="ZROT">
                        <Transform rotation='1 0 0 -1.57079632679489'>
                            <Inline url="http://dods.mbari.org/data/beds/x3d/beds_housing_with_axes.x3d"></Inline>
                        </Transform>
                    </Transform>
                </Transform>
            </Transform>
        </Transform>
    '''

    def __init__(self, kwargs, request, qs, qs_mp):
        self.kwargs = kwargs
        self.request = request
        self.qs = qs
        self.qs_mp = qs_mp      # Need the ordered version of the query set

    def loadData(self):
        '''
        Read the data from the database into member variables for construction of platform orientation time series
        '''
        # Save to '_by_act' dictionaries so that each time series can be separately controlled by ROUTEs or JavaScript controls to the orientation
        self.lon_by_act = {}
        self.lat_by_act = {}
        self.depth_by_act = {}
        self.time_by_act = {}

        self.roll_by_act = {}
        self.pitch_by_act = {}
        self.yaw_by_act = {}

        # Platform must have at least yaw in order for orientation data to make sense; must filter on one Parameter, otherwise we get multiple values
        # time_by_act is in Unix epoch milliseconds
        for mp in self.qs_mp.filter(parameter__standard_name='platform_yaw_angle'):
            self.lon_by_act.setdefault(mp['measurement__instantpoint__activity__name'], []).append(mp['measurement__geom'].x)
            self.lat_by_act.setdefault(mp['measurement__instantpoint__activity__name'], []).append(mp['measurement__geom'].y)
            self.depth_by_act.setdefault(mp['measurement__instantpoint__activity__name'], []).append(mp['measurement__depth'])

            # Need millisecond accuracy, add microseconds to what timetuple() provides (only to the second)
            dt = mp['measurement__instantpoint__timevalue']
            self.time_by_act.setdefault(mp['measurement__instantpoint__activity__name'], []).append(
                                      int((time.mktime(dt.timetuple()) + dt.microsecond / 1.e6) * 1000.0))

        for mp in self.qs_mp.filter(parameter__standard_name='platform_roll_angle'):
            self.roll_by_act.setdefault(mp['measurement__instantpoint__activity__name'], []).append(mp['datavalue'])
        for mp in self.qs_mp.filter(parameter__standard_name='platform_pitch_angle'):
            self.pitch_by_act.setdefault(mp['measurement__instantpoint__activity__name'], []).append(mp['datavalue'])
        for mp in self.qs_mp.filter(parameter__standard_name='platform_yaw_angle'):
            self.yaw_by_act.setdefault(mp['measurement__instantpoint__activity__name'], []).append(mp['datavalue'])

    def platformOrientationDataValuesForX3D(self, vert_ex=10.0, geoOrigin=''):
        '''
        Return the associated PlatformOrientation time series values as a dictionary of roll, pitch and yaw inside 
        a 2 level hash of platform__name and activity__name.  Results are in a format for easy update of an X3D scene graph.
        '''
        x3dDict = {}
        self.loadData()
        try:
            points = ''
            indices = ''
            index = 0
            gps = GPS()
            ##keys = ''
            translations = []
            for act in self.yaw_by_act.keys():
                for lon,lat,depth,t in izip( self.lon_by_act[act], self.lat_by_act[act], self.depth_by_act[act], self.time_by_act[act]):
                    if geoOrigin:
                        logger.warn('geoOrigin use is deprecated as X3DOM (post v1.6) now supports its use')
                        depth -= 45     # Temporary adjustment to make BED01 1-June-2013 event appear above terrain 
                        # TEST send translations directly to Transform from JavaScript
                        translations.append('%.1f,%.1f,%.1f' % gps.lla2gcc((lat, lon, -depth * vert_ex), geoOrigin))
                    else:
                        points += '%.6f %.6f %.1f ' % (lat, lon, -depth * vert_ex)

                    # If using Interpolators will need keys
                    ##keys += '%.4f' % ((t - self.time_by_act[act][0]) / (self.time_by_act[act][-1] - self.time_by_act[act][0]))
                    indices = indices + '%i ' % index
                    index = index + 1

                # No attempt to earth reference an 'up' orientation here    
                # TEST send rotations directly to Transforms from JavaScript
                xRotations = ['1,0,0,%.6f' % (np.pi * x / 180.) for x in self.roll_by_act[act]]
                yRotations = ['0,1,0,%.6f' % (np.pi * y / 180.) for y in self.pitch_by_act[act]]
                zRotations = ['0,0,1,%.6f' % (np.pi * -z / 180.) for z in self.yaw_by_act[act]]
                times = self.time_by_act[act]

                # End with -1 so that end point does not connect to the beg point
                indices = indices + '-1 ' 
                cycInt = '%.4f' % (self.time_by_act[act][-1] - self.time_by_act[act][0])

                x3dDict[act] = self.x3dEulerBaseScene

            if x3dDict:
                limits = (0, len(self.time_by_act[act]))

        except Exception as e:
            self.logger.exception('Could not create platformorientation')
            x3dResults = 'Could not create platformorientation'

        return {'x3d': x3dDict, 'limits': limits, 'translation': translations, 'time': times,
                'xRotation': xRotations, 'yRotation': yRotations, 'zRotation': zRotations}


class PPDatabaseException(Exception):
    def __init__(self, message, sql):
        Exception.__init__(self, message)
        self.sql = sql


class ParameterParameter(object):
    '''
    Use matploplib to create nice looking property-property plots
    '''
    logger = logging.getLogger(__name__)
    def __init__(self, request, pDict, mpq, pq, pMinMax):
        '''
        Save parameters that can be used by the different plotting methods here
        @pMinMax is like: (pID, pMin, pMax)
        '''
        self.request = request
        self.pDict = pDict
        self.mpq = mpq
        self.pq = pq
        self.pMinMax = pMinMax
        self.clt = readCLT(os.path.join(settings.STATIC_ROOT, 'colormaps', 'jetplus.txt'))
        self.depth = []
        self.x_id = []
        self.y_id = []
        self.x = []
        self.y = []
        self.z = []
        self.c = []
        self.lon = []
        self.lat = []
        self.sdepth = []
        self.sx = []
        self.sy = []
        self.sample_names = []

    def computeSigmat(self, limits, xaxis_name='sea_water_salinity', pressure=0):
        '''
        Given a tuple of limits = (xmin, xmax, ymin, ymax) and an xaxis_name compute potential
        density for a range of values between the mins and maxes.  Return the X and Y values
        for salinity/temperature and density converted to sigma-t. A pressure value may be passed
        to compute relative to a pressure other than 0.
        '''
        ns = 50
        nt = 50
        sigmat = []
        if xaxis_name == 'sea_water_salinity':
            s = np.linspace(limits[0], limits[1], ns, endpoint=False)
            t = np.linspace(limits[2], limits[3], nt, endpoint=False)
            for ti in t:
                row = []
                for si in s:
                    row.append(sw.pden(si, ti, pressure) - 1000.0)
                sigmat.append(row)

        elif xaxis_name == 'sea_water_temperature':
            t = np.linspace(limits[0], limits[1], nt, endpoint=False)
            s = np.linspace(limits[2], limits[3], ns, endpoint=False)
            for si in s:
                row = []
                for ti in t:
                    row.append(sw.pden(si, ti, pressure) - 1000.0)
                sigmat.append(row)

        else:
            raise Exception('Cannot compute sigma-t with xaxis_name = "%s"', xaxis_name)

        if xaxis_name == 'sea_water_salinity':
            return s, t, sigmat
        elif xaxis_name == 'sea_water_temperature':
            return t, s, sigmat

    def _getCountSQL(self, sql):
        '''
        Modify Parameter-Parameter SQL to return the count for the query
        '''
        p = re.compile('SELECT .+? FROM')           # Non-greedy, match to the first 'FROM'
        csql = p.sub('''SELECT count(*) FROM''', sql.replace('\n', ' '))
        self.logger.debug('csql = %s', csql)
        return csql

    def _getXYCData(self, strideFlag=True, latlonFlag=False, returnIDs=False, sampleFlag=True):
      @transaction.commit_on_success(using=self.request.META['dbAlias'])
      def inner_getXYCData(self, strideFlag, latlonFlag):
        '''
        Construct SQL and iterate through cursor to get X, Y, and possibly C Parameter Parameter data
        '''
        # Construct special SQL for P-P plot that returns up to 3 data values for the up to 3 Parameters requested for a 2D plot
        sql = str(self.pq.qs_mp.query)
        sql = self.pq.addParameterParameterSelfJoins(sql, self.pDict)
        if sampleFlag:
            sample_sql = self.pq.addSampleConstraint(sql)

        # Use cursors so that we can specify the database alias to use.
        cursor = connections[self.request.META['dbAlias']].cursor()
        sample_cursor = connections[self.request.META['dbAlias']].cursor()

        # Get count and set a stride value if more than a PP_MAX_POINTS which Matplotlib cannot plot, about 100,000 points
        try:
            cursor.execute(self._getCountSQL(sql))
        except DatabaseError, e:
            infoText = 'Parameter-Parameter: Cannot get count. Make sure you have no Parameters selected in the Filter.'
            self.logger.warn(e)
            raise PPDatabaseException(infoText, sql)

        pp_count = cursor.fetchone()[0]
        self.logger.debug('pp_count = %d', pp_count)
        stride_val = 1
        if strideFlag:
            PP_MAX_POINTS = 50000
            stride_val = int(pp_count / PP_MAX_POINTS)
            if stride_val < 1:
                stride_val = 1
            self.logger.debug('stride_val = %d', stride_val)

        if latlonFlag:
            if sql.find('stoqs_measurement') != -1:
                self.logger.debug('Adding lon lat to SELECT')
                sql = sql.replace('DISTINCT', 'DISTINCT ST_X(stoqs_measurement.geom) AS lon, ST_Y(stoqs_measurement.geom) AS lat,\n')
            elif sql.find('stoqs_sample') != -1:
                self.logger.debug('Adding lon lat to SELECT')
                sql = sql.replace('DISTINCT', 'DISTINCT ST_X(stoqs_sample.geom) AS lon, ST_Y(stoqs_sample.geom) AS lat,\n')

            if sampleFlag:
                sample_sql = sample_sql.replace('DISTINCT', 'DISTINCT ST_X(stoqs_sample.geom) AS lon, ST_Y(stoqs_sample.geom) AS lat,\n')

        if returnIDs:
            if sql.find('stoqs_measurement') != -1:
                self.logger.debug('Adding ids to SELECT for stoqs_measurement')
                sql = sql.replace('DISTINCT', 'DISTINCT mp_x.id, mp_y.id,\n')

        # Get the Parameter-Parameter points
        try:
            self.logger.debug('Executing sql = %s', sql)
            cursor.execute(sql)
        except DatabaseError, e:
            infoText = 'Parameter-Parameter: Query failed. Make sure you have no Parameters selected in the Filter.'
            self.logger.warn('Cannot execute sql query for Parameter-Parameter plot: %s', e)
            raise PPDatabaseException(infoText, sql)
       
        if sampleFlag: 
            # Get the Sample points
            try:
                self.logger.debug('Executing sample_sql = %s', sample_sql)
                sample_cursor.execute(sample_sql)
            except DatabaseError, e:
                infoText = 'Parameter-Parameter: Sample Query failed.'
                self.logger.warn('Cannot execute sample_sql query for Parameter-Parameter plot: %s', e)
                raise PPDatabaseException(infoText, sample_sql)

        # Populate MeasuredParameter x,y,c member variables
        counter = 0
        self.logger.debug('Looping through rows in cursor with a stride of %d...', stride_val)
        for row in cursor:
            if counter % stride_val == 0:
                # SampledParameter datavalues are Decimal, convert everything to a float for numpy
                lrow = list(row)
                if returnIDs:
                    self.x_id.append(int(lrow.pop(0)))
                    self.y_id.append(int(lrow.pop(0)))

                if latlonFlag:
                    self.lon.append(float(lrow.pop(0)))
                    self.lat.append(float(lrow.pop(0)))
                    
                self.depth.append(float(lrow.pop(0)))
                self.x.append(float(lrow.pop(0)))
                self.y.append(float(lrow.pop(0)))
                try:
                    self.c.append(float(lrow.pop(0)))
                except IndexError:
                    pass
            counter = counter + 1
            if counter % 1000 == 0:
                self.logger.debug('Made it through %d of %d points', counter, pp_count)

        if self.x == [] or self.y == []:
            raise PPDatabaseException('No data returned from query', sql)

        if sampleFlag:
            # Populate SampledParameter x,y,c member variables
            self.logger.debug('Looping through rows in sample_cursor')
            for row in sample_cursor:
                lrow = list(row)
                if latlonFlag:
                    self.lon.append(float(lrow.pop(0)))
                    self.lat.append(float(lrow.pop(0)))
    
                # Need only the x and y values for sample points                
                self.sdepth.append(float(lrow.pop(0)))
                self.sx.append(float(lrow.pop(0)))
                self.sy.append(float(lrow.pop(0)))
                self.sample_names.append(lrow.pop(0))

        return stride_val, sql, pp_count

      return inner_getXYCData(self, strideFlag, latlonFlag)

    def make2DPlot(self):
        '''
        Produce a Parameter-Parameter .png image with axis limits set to the 1 and 99 percentiles and draw outside the lines
        '''
        pplrFlag = self.request.GET.get('pplr', False)
        ppfrFlag = self.request.GET.get('ppfr', False)
        ppslFlag = self.request.GET.get('ppsl', False)
     
        sql = ''
        try:
            # self.x and self.y may already be set for this instance by makeX3D()
            if not self.x and not self.y:
                stride_val, sql, pp_count = self._getXYCData()

            # If still no self.x and self.y then selection is not valid for the chosen x and y
            if self.x == [] or self.y == []:
                return None, 'No Parameter-Parameter data values returned.', sql
            
            # Use session ID so that different users don't stomp on each other with their parameterparameter plots
            # - This does not work for Firefox which just reads the previous image from its cache
            if self.request.session.has_key('sessionID'):
                sessionID = self.request.session['sessionID']
            else:
                sessionID = ''.join(random.choice(string.ascii_uppercase + string.digits) for x in range(7))
                self.request.session['sessionID'] = sessionID
            # - Use a new imageID for each new image
            imageID = ''.join(random.choice(string.ascii_uppercase + string.digits) for x in range(10))
            ppPngFile = '%s_%s_%s_%s.png' % (self.pDict['x'], self.pDict['y'], self.pDict['c'], imageID)
            ppPngFileFullPath = os.path.join(settings.MEDIA_ROOT, 'parameterparameter', ppPngFile)
            if not os.path.exists(os.path.dirname(ppPngFileFullPath)):
                try:
                    os.makedirs(os.path.dirname(ppPngFileFullPath))
                except Exception,e:
                    self.logger.exception('Failed to create path for ' +
                                     'parameterparameter (%s) file', ppPngFile)
                    return None, 'Failed to create path for parameterparameter (%s) file' % ppPngFile, sql

            # Make the figure
            fig = plt.figure()
            plt.grid(True)
            ax = fig.add_subplot(111)
            if not ppfrFlag:
                ax.set_xlim(self.pMinMax['x'][1], self.pMinMax['x'][2])
                ax.set_ylim(self.pMinMax['y'][1], self.pMinMax['y'][2])

            self.clt = readCLT(os.path.join(settings.STATIC_ROOT, 'colormaps', 'jetplus.txt'))
            cm_jetplus = mpl.colors.ListedColormap(np.array(self.clt))
            if self.c:
                self.logger.debug('self.pMinMax = %s', self.pMinMax)
                self.logger.debug('Making colored scatter plot of %d points', len(self.x))
                ax.scatter(self.x, self.y, c=self.c, s=10, cmap=cm_jetplus, lw=0, vmin=self.pMinMax['c'][1], vmax=self.pMinMax['c'][2], clip_on=False)
                # Add colorbar to the image
                cb_ax = fig.add_axes([0.2, 0.98, 0.6, 0.02]) 
                norm = mpl.colors.Normalize(vmin=self.pMinMax['c'][1], vmax=self.pMinMax['c'][2], clip=False)
                cb = mpl.colorbar.ColorbarBase( cb_ax, cmap=cm_jetplus,
                                                norm=norm,
                                                ticks=list(np.linspace(self.pMinMax['c'][1], self.pMinMax['c'][2], num=4)),
                                                orientation='horizontal')
                try:
                    cp = models.Parameter.objects.using(self.request.META['dbAlias']).get(id=int(self.pDict['c']))
                except ValueError:
                    # Likely a coordinate variable
                    cp = models.Parameter
                    cp.name = self.pDict['c']
                    cp.standard_name = self.pDict['c']
                    cp.units = _getCoordUnits(self.pDict['c'])
                cb.set_label('%s (%s)' % (cp.name, cp.units))
            else:
                self.logger.debug('Making scatter plot of %d points', len(self.x))
                ax.scatter(self.x, self.y, marker='.', s=10, c='k', lw = 0, clip_on=False)

            # Label the axes
            try:
                xp = models.Parameter.objects.using(self.request.META['dbAlias']).get(id=int(self.pDict['x']))
            except ValueError:
                # Likely a coordinate variable
                xp = models.Parameter
                xp.name = self.pDict['x']
                xp.standard_name = self.pDict['x']
                xp.units = _getCoordUnits(self.pDict['x'])
            ax.set_xlabel('%s (%s)' % (xp.name, xp.units))

            try:
                yp = models.Parameter.objects.using(self.request.META['dbAlias']).get(id=int(self.pDict['y']))
            except ValueError:
                # Likely a coordinate variable
                yp = models.Parameter
                yp.name = self.pDict['y']
                yp.standard_name = self.pDict['y']
                yp.units = _getCoordUnits(self.pDict['y'])
                if self.pDict['y'] == 'depth':
                    ax.invert_yaxis()
            ax.set_ylabel('%s (%s)' % (yp.name, yp.units))

            # Add Sigma-t contours if x/y is salinity/temperature, approximate depth to pressure - must fix for deep water...
            Z = None
            infoText = 'n = %d' % len(self.x)
            if stride_val > 1:
                infoText += ' (of %d, stride = %d)' % (pp_count, stride_val)
            infoText += '<br>%s ranges: fixed [%f, %f], actual [%f, %f]<br>%s ranges: fixed [%f, %f], actual [%f, %f]' % (
                            xp.name, self.pMinMax['x'][1], self.pMinMax['x'][2], np.min(self.x), np.max(self.x),
                            yp.name, self.pMinMax['y'][1], self.pMinMax['x'][2], np.min(self.y), np.max(self.y))
            if xp.standard_name == 'sea_water_salinity' and yp.standard_name == 'sea_water_temperature':
                X, Y, Z = self.computeSigmat(ax.axis(), xaxis_name='sea_water_salinity')
            if xp.standard_name == 'sea_water_temperature' and yp.standard_name == 'sea_water_salinity':
                X, Y, Z = self.computeSigmat(ax.axis(), xaxis_name='sea_water_temperature')
            if Z is not None:
                CS = ax.contour(X, Y, Z, colors='k')
                plt.clabel(CS, inline=1, fontsize=10)
   
            if pplrFlag: 
                # Do Linear regression and assemble additional information about the correlation
                self.logger.debug('polyfit')
                m, b = polyfit(self.x, self.y, 1)
                self.logger.debug('polyval')
                yfit = polyval([m, b], self.x)
                ax.plot(self.x, yfit, color='k', linewidth=0.5)
                c = np.corrcoef(self.x, self.y)[0,1]
                pr = pearsonr(self.x, self.y)
                ##test_pr = pearsonr([1,2,3], [1,5,7])
                ##self.logger.debug('test_pr = %f (should be 0.981980506062)', test_pr)
                infoText += '<br>Linear regression: %s = %s * %s + %s (r<sup>2</sup> = %s, p = %s)' % (yp.name, 
                                round_to_n(m,4), xp.name, round_to_n(b,4), round_to_n(c**2,4), round_to_n(pr,4))

            # Add any sample locations
            if ppslFlag:
                if self.sx and self.sy:
                    if self.c:
                        try:
                            ax.scatter(self.sx, self.sy, marker='o', c=self.c, s=25, cmap=cm_jetplus, vmin=self.pMinMax['c'][1], vmax=self.pMinMax['c'][2], clip_on=False)
                        except ValueError, e:
                            # Likely because a Measured Parameter has been selected for color
                            logger.warn(e)
                            ax.scatter(self.sx, self.sy, marker='o', c='w', s=25, zorder=10, clip_on=False)
                    else:
                        ax.scatter(self.sx, self.sy, marker='o', c='w', s=25, zorder=10, clip_on=False)
                    for i, txt in enumerate(self.sample_names):
                        ax.annotate(txt, xy=(self.sx[i], self.sy[i]), xytext=(3.0, 3.0), textcoords='offset points')
            
            # Save the figure
            try:
                self.logger.debug('Saving to file ppPngFileFullPath = %s', ppPngFileFullPath)
                fig.savefig(ppPngFileFullPath, dpi=120, transparent=True)
            except Exception, e:
                infoText = 'Parameter-Parameter: ' + str(e)
                self.logger.exception('Cannot make 2D parameterparameter plot: %s', e)
                plt.close()
                return None, infoText, sql

        except TypeError, e:
            ##infoText = 'Parameter-Parameter: ' + str(type(e))
            infoText = 'Parameter-Parameter: ' + str(e)
            self.logger.exception('Cannot make 2D parameterparameter plot: %s', e)
            plt.close()
            return None, infoText, sql

        else:
            plt.close()
            return ppPngFile, infoText, sql

    def makeX3D(self):
      @transaction.commit_on_success(using=self.request.META['dbAlias'])
      def inner_makeX3D(self):
        '''
        Produce X3D XML text and return it
        '''
        x3dResults = {}
        try:
            # Construct special SQL for P-P plot that returns up to 4 data values for the up to 4 Parameters requested for a 3D plot
            sql = str(self.pq.qs_mp.query)
            self.logger.debug('self.pDict = %s', self.pDict)
            sql = self.pq.addParameterParameterSelfJoins(sql, self.pDict)

            # Use cursor so that we can specify the database alias to use. Columns are always 0:x, 1:y, 2:c (optional)
            cursor = connections[self.request.META['dbAlias']].cursor()
            cursor.execute(sql)
            for row in cursor:
                # SampledParameter datavalues are Decimal, convert everything to a float for numpy, row[0] is depth
                self.depth.append(float(row[0]))
                self.x.append(float(row[1]))
                self.y.append(float(row[2]))
                self.z.append(float(row[3]))
                try:
                    self.c.append(float(row[4]))
                except IndexError:
                    pass

            if self.c:
                self.c.reverse()    # Modifies self.c in place - needed for popping values off in loop below

            points = ''
            colors = ''
            for x,y,z in izip(self.x, self.y, self.z):
                # Scale to 10000 on each axis, bounded by min/max values - must be 10000 as X3D in stoqs/templates/stoqsquery.html is hard-coded with 10000
                # This gives us enough resolution for modern displays and eliminates decimal point characters
                xs = 10000 * (x - float(self.pMinMax['x'][1])) / (float(self.pMinMax['x'][2]) - float(self.pMinMax['x'][1])) 
                ys = 10000 * (y - float(self.pMinMax['y'][1])) / (float(self.pMinMax['y'][2]) - float(self.pMinMax['y'][1])) 
                zs = 10000 * (z - float(self.pMinMax['z'][1])) / (float(self.pMinMax['z'][2]) - float(self.pMinMax['z'][1])) 
                points = points + '%d %d %d ' % (int(xs), int(ys), int(zs))
                if self.c:
                    cindx = int(round((self.c.pop() - float(self.pMinMax['c'][1])) * (len(self.clt) - 1) / 
                                    (float(self.pMinMax['c'][2]) - float(self.pMinMax['c'][1]))))
                    if cindx < 0:
                        cindx = 0
                    if cindx > len(self.clt) - 1:
                        cindx = len(self.clt) - 1
                    colors = colors + '%.3f %.3f %.3f ' % (self.clt[cindx][0], self.clt[cindx][1], self.clt[cindx][2])
                else:
                    colors = colors + '0 0 0 '

            # Label the axes
            try:
                xp = models.Parameter.objects.using(self.request.META['dbAlias']).get(id=int(self.pDict['x']))
            except ValueError:
                # Likely a coordinate variable
                xp = models.Parameter
                xp.name = self.pDict['x']
                xp.standard_name = self.pDict['x']
                xp.units = _getCoordUnits(self.pDict['x'])
            self.pMinMax['x'].append(('%s (%s)' % (xp.name, xp.units)))

            try:
                yp = models.Parameter.objects.using(self.request.META['dbAlias']).get(id=int(self.pDict['y']))
            except ValueError:
                # Likely a coordinate variable
                yp = models.Parameter
                yp.name = self.pDict['y']
                yp.standard_name = self.pDict['y']
                yp.units = _getCoordUnits(self.pDict['y'])
            self.pMinMax['y'].append(('%s (%s)' % (yp.name, yp.units)))

            try:
                zp = models.Parameter.objects.using(self.request.META['dbAlias']).get(id=int(self.pDict['z']))
            except ValueError:
                # Likely a coordinate variable
                zp = models.Parameter
                zp.name = self.pDict['z']
                zp.standard_name = self.pDict['z']
                zp.units = _getCoordUnits(self.pDict['z'])
            self.pMinMax['z'].append(('%s (%s)' % (zp.name, zp.units)))

            colorbarPngFile = ''
            if self.pDict['c']:
                try:
                    cp = models.Parameter.objects.using(self.request.META['dbAlias']).get(id=int(self.pDict['c']))
                except ValueError:
                    # Likely a coordinate variable
                    cp = models.Parameter
                    cp.name = self.pDict['c']
                    cp.standard_name = self.pDict['c']
                    cp.units = _getCoordUnits(self.pDict['c'])
                try:
                    cm_jetplus = mpl.colors.ListedColormap(np.array(self.clt))
                    imageID = ''.join(random.choice(string.ascii_uppercase + string.digits) for x in range(10))
                    colorbarPngFile = '%s_%s_%s_%s_3dcolorbar_%s.png' % (self.pDict['x'], self.pDict['y'], self.pDict['z'], self.pDict['c'], imageID )
                    colorbarPngFileFullPath = os.path.join(settings.MEDIA_ROOT, 'parameterparameter', colorbarPngFile)
                    makeColorBar(self.request, colorbarPngFileFullPath, self.pMinMax['c'], cm_jetplus)

                except Exception,e:
                    self.logger.exception('Could not plot the colormap')
                    return None, None, 'Could not plot the colormap'

            x3dResults = {'colors': colors, 'points': points, 'info': '', 'x': self.pMinMax['x'], 'y': self.pMinMax['y'], 'z': self.pMinMax['z'], 
                          'colorbar': colorbarPngFile, 'sql': sql}

        except DatabaseError:
            self.logger.exception('Cannot make parameterparameter X3D')
            raise DatabaseError('Cannot make parameterparameter X3D')

        return x3dResults

      return inner_makeX3D(self)

