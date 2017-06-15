# -*- coding: utf-8 -*-
"""
Created on Fri Dec 02 15:02:14 2016
A command line tool for conducting Zonal Statistics in SciDB

@author: dahaynes
"""


from osgeo import ogr, gdal
import scidbpy, timeit, csv, argparse, os, re
from collections import OrderedDict

def world2Pixel(geoMatrix, x, y):
    """
    Uses a gdal geomatrix (gdal.GetGeoTransform()) to calculate
    the pixel location of a geospatial coordinate
    """
    ulX = geoMatrix[0]
    ulY = geoMatrix[3]
    xDist = geoMatrix[1]
    yDist = geoMatrix[5]
    rtnX = geoMatrix[2]
    rtnY = geoMatrix[4]
    pixel = int((x - ulX) / xDist)
    line = int((ulY - y) / xDist)
    
    return (pixel, line)
    

def RasterizePolygon(inRasterPath, outRasterPath, vectorPath):
    """
    This function will Rasterize the Polygon based off the inRasterPath provided. 
    This only creates a memory raster
    The rasterization process uses the shapfile attribute ID
    """
    
    #The array size, sets the raster size 
    inRaster = gdal.Open(inRasterPath)
    rasterTransform = inRaster.GetGeoTransform()
    pixel_size = rasterTransform[1]
    
    #Open the vector dataset
    vector_dataset = ogr.Open(vectorPath)
    theLayer = vector_dataset.GetLayer()
    geomMin_X, geomMax_X, geomMin_Y, geomMax_Y = theLayer.GetExtent()
    
    outTransform= [geomMin_X, rasterTransform[1], 0, geomMax_Y, 0, rasterTransform[5] ]
    
    rasterWidth = int((geomMax_X - geomMin_X) / pixel_size)
    rasterHeight = int((geomMax_Y - geomMin_Y) / pixel_size)

    memDriver = gdal.GetDriverByName('MEM')
    theRast = memDriver.Create('', rasterWidth, rasterHeight, 1, gdal.GDT_Int16)
      
    theRast.SetProjection(inRaster.GetProjection())
    theRast.SetGeoTransform(outTransform)
    
    band = theRast.GetRasterBand(1)
    band.SetNoDataValue(-999)

    #If you want to use another shapefile field you need to change this line
    gdal.RasterizeLayer(theRast, [1], theLayer, options=["ATTRIBUTE=ID"])
    
    bandArray = band.ReadAsArray()
    del theRast, inRaster

    return bandArray

def GlobalJoin_SummaryStats(sdb, SciDBArray, rasterValueDataType, tempSciDBLoad, tempRastName, xMin, yMin, xMax, yMax, verbose=False):
    """
    Trying to figure this out
    1. Make an empty raster "Mask "that matches the SciDBArray
    2. Load the data into a 1D array
    3. Redimension and insert data into the mask array
    4. Conduct a global join using the between operators
    """
    import re
    afl = sdb.afl
    tempArray = "mask"

    theArray = afl.show(SciDBArray)
    results = theArray.contents()
    #SciDBArray()\n[('polygon<x:int64,y:int64,id:int16> [xy=0:*:0:1000000]')]\n
    #[('GLC2000<value:uint8> [x=0:40319:0:100000; y=0:16352:0:100000]')]
    
    #R = re.compile(r'\<(?P<attributes>[\S\s]*?)\>(\s*)\[(?P<dim_1>\S+)(;\s|,\s)(?P<dim_2>\S+)(\])')
    R = re.compile(r'\<(?P<attributes>[\S\s]*?)\>(\s*)\[(?P<dim_1>\S+)(;\s|,\s)(?P<dim_2>[^\]]+)')
    results = results.lstrip('results').strip()
    match = R.search(results)
    
    try:
        A = match.groupdict()
        schema = A['attributes']
        dimensions = "[%s; %s]" % (A['dim_1'], A['dim_2'])
    except:
        print(results)
        raise 

    try:
        sdbquery = r"create array %s <id:%s> %s" % (tempArray, rasterValueDataType, dimensions)
        sdb.query(sdbquery)
    except:
        sdb.query("remove(%s)" % tempArray)
        sdbquery = r"create array %s <id:%s> %s" % (tempArray, rasterValueDataType, dimensions)
        sdb.query(sdbquery)

    LoadArraytoSciDB(sdb, tempSciDBLoad, tempRastName, rasterValueDataType, "x1", "y1", verbose)
    
    #Write the array in the correct location
    start = timeit.default_timer()
    sdbquery ="insert(redimension(apply({A}, x, x1+{yOffSet}, y, y1+{xOffset}, value, id), {B} ), {B})".format( A=tempRastName, B=tempArray, yOffSet=yMin, xOffset=xMin)
    sdb.query(sdbquery)
    stop = timeit.default_timer()
    insertTime = stop-start
    if verbose: print(sdbquery , insertTime)
    
    #between(GLC2000, 4548, 6187, 7332, 12662)
    start = timeit.default_timer()
    sdbquery = "grouped_aggregate(join(between(%s, %s, %s, %s, %s), between(%s, %s, %s, %s, %s)), min(value), max(value), avg(value), count(value), id)" % (SciDBArray, yMin, xMin, yMax, xMax, tempArray, yMin, xMin, yMax, xMax)
    sdb.query(sdbquery)
    stop = timeit.default_timer()
    queryTime = stop-start
    if verbose: print(sdbquery, queryTime)

    return insertTime, queryTime
    

def WriteMultiDimensionalArray(rArray, csvPath, xOffset=0, yOffset=0 ):
    '''
    This function write the multidimensional array as a binary 
    '''
    import numpy as np
    with open(csvPath, 'wb') as fileout:
        arrayHeight, arrayWidth = rArray.shape
        it = np.nditer(rArray, flags=['multi_index'], op_flags=['readonly'])
        for counter, pixel in enumerate(it):
            col, row = it.multi_index
            #if counter < 100: print("y/column: %s, x/row: %s" % (col + yOffset, row + xOffset))
            indexvalue = np.array([col + yOffset, row + xOffset], dtype=np.dtype('int64'))

            fileout.write( indexvalue.tobytes() )
            fileout.write( it.value.tobytes() )
   
    return(arrayHeight, arrayWidth)


def WriteFile(filePath, theDictionary):
    """
    This function writes out the dictionary as csv
    """
    
    thekeys = list(theDictionary.keys())
    
    with open(filePath, 'w') as csvFile:
        fields = list(theDictionary[thekeys[0]].keys())
        #fields.append("test")
        #print(fields)
        theWriter = csv.DictWriter(csvFile, fieldnames=fields)
        theWriter.writeheader()

        for k in theDictionary.keys():
            #theDictionary[k].update({"test": k})
            #print(theDictionary)
            theWriter.writerow(theDictionary[k])

def QueryResults():
    """
    Function to perform the Zonal Analysis can get back the results
    """

    afl = sdb.afl
    result = afl.grouped_aggregate(afl.join(polygonSciDBArray.name, afl.subarray(SciDBArray, ulY, ulX, lrY, lrX)), max("value"), "f0")
    #query = "grouped_aggregate(join(%s,subarray(%s, %s, %s, %s, %s)), min(value), max(value), avg(value), count(value), f0)" % (polygonSciDBArray.name, SciDBArray, ulY, ulX, lrY, lrX)


def LoadArraytoSciDB(sdb, tempSciDBLoad, tempRastName, rasterValueDataType, dim1="x", dim2="y", verbose=False):
    """
    Function Loads 1D array data into sciDB
    in : 
        sdb connection
        tempSciDBLoad - path for loading scidbdata
        tempRastName - Name for loading raster dataset
        rasterValeDataType - Numpy value type
        dim1 = name of the dimension (default = x) 
        dim2 = name of the dimension (default = y) 
    out : 
        binaryLoadPath : complete path to where the file is written (*.scidb)
    """

    binaryLoadPath = '%s/%s.scidb' % (tempSciDBLoad,tempRastName )
    try:
        sdbquery = "create array %s <%s:int64, %s:int64, id:%s> [xy=0:*,?,?]" % (tempRastName, dim1, dim2, rasterValueDataType)
        sdb.query(sdbquery)
    except:
        sdb.query("remove(%s)" % (tempRastName))
        sdbquery = "create array %s <%s:int64, %s:int64, id:%s> [xy=0:*,?,?]" % (tempRastName, dim1, dim2, rasterValueDataType)
        sdb.query(sdbquery)

    start = timeit.default_timer()
    sdbquery = "load(%s,'%s', -2, '(int64, int64, %s)' )" % (tempRastName, binaryLoadPath, rasterValueDataType)
    sdb.query(sdbquery)
    stop = timeit.default_timer()
    loadTime = stop-start
    if verbose: print(sdbquery , loadTime)

    return binaryLoadPath, loadTime

def EquiJoin_SummaryStats(sdb, SciDBArray, tempRastName, rasterValueDataType, tempSciDBLoad, ulY, lrY, ulX, lrX, verbose=False):
    """
    1. Load the polygon array in as a 1D array, shifted correctly
    2. Peform EquiJoin using the between
    Example (equi_join(between(GLC2000, 4548, 6187, 7331, 12661)
    grouped_aggregate(equi_join(between(GLC2000, 4548, 6187, 7332, 12662), polygon), 'left_names=x,y', 'right_names=x,y'), min(value), max(value), avg(value), count(value), id)

    """

    binaryLoadPath, loadTime = LoadArraytoSciDB(sdb, tempSciDBLoad, tempRastName, rasterValueDataType, 'x', 'y', verbose)
    
    start = timeit.default_timer()
    sdbquery = "grouped_aggregate(equi_join(between(%s, %s, %s, %s, %s), %s, 'left_names=x,y', 'right_names=x,y'), min(value), max(value), avg(value), count(value), id)" % (SciDBArray, ulY, ulX, lrY, lrX, tempRastName) 
    if verbose: print(sdbquery)
    sdb.query(sdbquery) 
    stop = timeit.default_timer()
    queryTime = stop-start

    return loadTime, queryTime


def ZonalStats(NumberofTests, boundaryPath, rasterPath, SciDBArray, statsMode=1, filePath=None, verbose=False):
    "This function conducts zonal stats in SciDB"
    
    outDictionary = OrderedDict()
    sdb = scidbpy.connect()

    for t in range(NumberofTests):
        theTest = "test_%s" % (t+1)
        #outDictionary[theTest]

        vectorFile = ogr.Open(boundaryPath)
        theLayer = vectorFile.GetLayer()
        geomMin_X, geomMax_X, geomMin_Y, geomMax_Y = theLayer.GetExtent()

        inRaster = gdal.Open(rasterPath)
        rasterTransform = inRaster.GetGeoTransform()

        start = timeit.default_timer()
        rasterizedArray = RasterizePolygon(rasterPath, r'/home/scidb/scidb_data/0/0/nothing.tiff', boundaryPath)
        rasterValueDataType = rasterizedArray.dtype
        stop = timeit.default_timer()
        rasterizeTime = stop-start
        print("Rasterization time %s for file %s" % (rasterizeTime, boundaryPath ))
        

        ulX, ulY = world2Pixel(rasterTransform, geomMin_X, geomMax_Y)
        lrX, lrY = world2Pixel(rasterTransform, geomMax_X, geomMin_Y)
        
        if verbose:
            print("Rasterized Array columns:%s, rows: %s" % (rasterizedArray.shape[0], rasterizedArray.shape[1]))
            print("ulX:%s, ulY:%s, lrX:%s, lrY:%s" % ( ulX, ulY, lrX, lrY))

        if statsMode == 1:
            #Transfering Raster Array to SciDB
            start = timeit.default_timer()
            polygonSciDBArray = sdb.from_array(rasterizedArray, instance_id=0, name="states", persistent=False, chunk_size=100000) 

            #polygonSciDBArray = sdb.from_array(rasterizedArray, dim_low=(4000,5000), dim_high=(5000,7000), instance_id=0, chunk_size=1000) 
            #name="states"

            stop = timeit.default_timer()
            transferTime = stop-start
            if verbose: print(transferTime)

            #Raster Summary Stats
            query = "grouped_aggregate(join(%s,subarray(%s, %s, %s, %s, %s)), min(value), max(value), avg(value), count(value), f0)" % (polygonSciDBArray.name, SciDBArray, ulX, ulY, lrX, lrY)
            start = timeit.default_timer()
            if verbose: print(query)
            results = sdb.query(query)
            stop = timeit.default_timer()
            queryTime = stop-start

        elif statsMode == 2:
            csvPath = '/home/scidb/scidb_data/0/0/polygon.scidb'
            WriteMultiDimensionalArray(rasterizedArray, csvPath, ulX, ulY )    
            tempRastName = csvPath.split('/')[-1].split('.')[0]
            tempSciDBLoad = '/'.join(csvPath.split('/')[:-1])
            transferTime, queryTime = EquiJoin_SummaryStats(sdb, SciDBArray, tempRastName, rasterValueDataType, tempSciDBLoad, ulY, lrY, ulX, lrX, verbose)

        elif statsMode == 3:
            csvPath = '/home/scidb/scidb_data/0/0/zones.scidb'
            WriteMultiDimensionalArray(rasterizedArray, csvPath)
            tempSciDBLoad = '/'.join(csvPath.split('/')[:-1])
            tempRastName = csvPath.split('/')[-1].split('.')[0]
            transferTime, queryTime = GlobalJoin_SummaryStats(sdb, SciDBArray, rasterValueDataType, tempSciDBLoad, tempRastName, ulX, ulY, lrX, lrY, verbose)
            
        
        print("Zonal Analyis time %s, for file %s, Query run %s " % (queryTime, boundaryPath, t+1 ))
        if verbose: print("TransferTime: %s" % (transferTime)  )
        outDictionary[theTest] = OrderedDict( [ ("test",theTest), ("SciDBArrayName",SciDBArray), ("BoundaryFilePath",boundaryPath), ("transfer_time",transferTime), ("rasterization_time",rasterizeTime), ("query_time",queryTime), ("total_time",transferTime+rasterizeTime+queryTime) ] )
    

    sdb.reap()
    if filePath:
        WriteFile(filePath, outDictionary)
    print("Finished")


def CheckFiles(*argv):
    "This function checks files to make sure they exist"
    for i in argv:
        if not os.path.exists(i): 
            print("FilePath %s does not exist" % (i) )
            return False
    return True

def argument_parser():
    parser = argparse.ArgumentParser(description="Conduct SciDB Zonal Stats")   
    parser.add_argument('-SciDBArray', required=True, dest='SciArray')
    parser.add_argument('-Raster', required=True, dest='Raster')
    parser.add_argument('-Shapefile', required=True, dest='Shapefile')
    parser.add_argument('-Tests', type=int, help="Number of tests you want to run", required=False, default=3, dest='Runs')
    parser.add_argument('-Mode', help="This allows you to choose the mode of analysis you want to conduct", type=int, default=1, required=True, dest='mode')
    parser.add_argument('-CSV', required=False, dest='CSV')
    parser.add_argument('-v', required=False, action="store_true", dest='verbose')
    
    return parser

if __name__ == '__main__':
    args = argument_parser().parse_args()
    if CheckFiles(args.Shapefile, args.Raster):
        ZonalStats(args.Runs, args.Shapefile, args.Raster, args.SciArray, args.mode, args.CSV, args.verbose)
    # else:
    #     print(args)

