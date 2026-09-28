/*
 * Copyright Car Connectivity Consortium
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 *
 * You may decide to give the Car Connectivity Consortium input, suggestions
 * or feedback of a technical nature which may be implemented on the
 * Car Connectivity Consortium products (“Feedback”).
 *
 * You agrees that any such Feedback is given on non-confidential
 * basis and Licensee hereby waives any confidentiality restrictions
 * for such Feedback. In addition, Licensee grants to the Car Connectivity Consortium
 * and its affiliates a worldwide, non-exclusive, perpetual, irrevocable,
 * sub-licensable, royalty-free right and license under Licensee’s copyrights to copy,
 * reproduce, modify, create derivative works and directly or indirectly
 * distribute, make available and communicate to public the Feedback
 * in or in connection to any CCC products, software and/or services.
 */
package com.carconnectivity.testapp;


import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;

import android.app.AlertDialog;
import android.os.Bundle;
import android.os.RemoteException;
import android.view.View;
import android.view.View.MeasureSpec;
import android.view.View.OnClickListener;
import android.view.ViewGroup;
import android.widget.AdapterView;
import android.widget.AdapterView.OnItemClickListener;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.ListView;
import android.widget.ScrollView;
import android.widget.SimpleAdapter;
import android.widget.Toast;

import com.carconnectivity.testapp.views.HeaderView;
import com.carconnectivity.testapp.views.LastExecutedView;
import com.carconnectivity.testapp.views.SuccesView;
import com.carconnectivity.testapp.views.TextViewString;
import com.mirrorlink.android.commonapi.Defs;
import com.mirrorlink.android.commonapi.ICertificationManager;

public class ApplicationCertificateStatus extends BaseActivity {
	SuccesView certificateAvailable = null;
	SuccesView advertisedAsCertifiedApp = null;
	LastExecutedView timestampOfLastUpdate = null;
	Button getApplicationCertification = null;
	TextViewString entitiesTextView = null;
	LastExecutedView timestampOfLastEntitiesUpdate = null;
	Button getAppCertEntities =null;
	
	ListView entitiesListView = null;
	
	ICertificationManager certificationManager = null;
	
	@Override
	protected void onResume()
	{
		certificationManager = getMirrorLinkApplicationContext().registerCertificationManager(this, null);
		if (certificationManager== null)
		{
			Toast.makeText(this, "Unable to get certification manager.", Toast.LENGTH_LONG).show();
			finish();
		}
		super.onResume();

	}
	
	@Override
	protected void onPause()
	{
		getMirrorLinkApplicationContext().unregisterCertificationManager(this, null);
		super.onPause();
	}
	
	
	
	

	@Override
	void updateValues()
	{
		updateAppCertStatus();
		updateEntites();

	}
	void updateAppCertStatus()
	{
		
		if(certificationManager!= null)
		{
			Bundle result = null;
			try {
				result = certificationManager.getApplicationCertificationStatus();
			} catch (RemoteException e) {
				e.printStackTrace();
			}
			timestampOfLastUpdate.setNow();
			certificateAvailable.setValue(result.getBoolean(Defs.ApplicationCertificationStatus.HAS_VALID_CERTIFICATE));
			advertisedAsCertifiedApp.setValue(result.getBoolean(Defs.ApplicationCertificationStatus.ADVERTISED_AS_CERTIFIEDAPP));
		}
		
	}
	
	void updateEntites()
	{
		if(certificationManager!= null)
		{
			String entities = "";
			try {
				entities = certificationManager.getApplicationCertifyingEntities();
			} catch (RemoteException e) {
				e.printStackTrace();
			}
			if (entities != null)
			{
				List<HashMap<String, String>> entietiesList = new ArrayList<HashMap<String, String>>();
				 
				String[] entietiesListString = entities.split(",");
				
				for(int i = 0; i < entietiesListString.length; i++){
					HashMap<String, String> map = new HashMap<String, String>();
					map.put("text", entietiesListString[i].trim());
					entietiesList.add(map);
				}

				SimpleAdapter adapter = new SimpleAdapter(this,entietiesList, android.R.layout.simple_list_item_1, new String[] {"text"}, new int[] {android.R.id.text1} );
				entitiesListView.setAdapter(adapter);
				
				// Workaround for listView in scroll view
				// http://nex-otaku-en.blogspot.com/2010/12/android-put-listview-in-scrollview.html
				
				int totalHeight = 0;
				for (int i = 0; i < adapter.getCount(); i++) {
		            View listItem = adapter.getView(i, null, entitiesListView);
		            listItem.measure(0, MeasureSpec.UNSPECIFIED);
		            totalHeight += listItem.getMeasuredHeight();
		        }
				
				ViewGroup.LayoutParams params = entitiesListView.getLayoutParams();
		        params.height = totalHeight + (entitiesListView.getDividerHeight() * (entitiesListView.getCount() - 1));
		        entitiesListView.setLayoutParams(params);
				timestampOfLastEntitiesUpdate.setNow();
			}
			else
			{
				Toast.makeText(this, "Unable to get application certifying entities", Toast.LENGTH_SHORT).show();;
			}
			
			
		}
	}
	
	ScrollView buildUI()
	{
		ScrollView scrollView = new ScrollView(this);
		LinearLayout layout = new LinearLayout(this);
		layout.setOrientation(LinearLayout.VERTICAL);
		
		HeaderView appCertStatusHeader = new HeaderView(this,"Application Certificate Status");
		layout.addView(appCertStatusHeader);

		certificateAvailable = new SuccesView(this,"Certificate available",false);
		layout.addView(certificateAvailable);
		advertisedAsCertifiedApp  = new SuccesView(this,"Advertised as Certified app",false);
		layout.addView(advertisedAsCertifiedApp);
		timestampOfLastUpdate = new LastExecutedView(this);
		layout.addView(timestampOfLastUpdate);
		getApplicationCertification = new Button(this);
		getApplicationCertification.setText("Get Application Certification Status");
		layout.addView(getApplicationCertification);
		getApplicationCertification.setOnClickListener(new OnClickListener() {
			@Override
			public void onClick(View arg0) {
				updateValues();
			}
		});
		
		HeaderView appCertEntitiesHeader = new HeaderView(this,"Certificate Certifying entities");
		layout.addView(appCertEntitiesHeader);
		
		entitiesListView = new ListView(this);
		layout.addView(entitiesListView);
		
		entitiesListView.setOnItemClickListener(new OnItemClickListener() {
			@Override
			public void onItemClick(AdapterView<?> adapter, View arg1, int pos,	long arg3) 
			{
				@SuppressWarnings("unchecked")
				HashMap<String, String> entityName = (HashMap<String, String>) adapter.getItemAtPosition(pos);
				showAppCertInfoDialog(entityName.get("text"));
			}
		});

		timestampOfLastEntitiesUpdate = new LastExecutedView(this);
		layout.addView(timestampOfLastEntitiesUpdate);

		getAppCertEntities = new Button(this);
		getAppCertEntities.setText("Get App Certifing Entities");
		layout.addView(getAppCertEntities);

		getAppCertEntities.setOnClickListener(new OnClickListener() {
			@Override
			public void onClick(View v) {
				updateEntites();
			}
		});
		
		
		scrollView.addView(layout);
		return scrollView;
	}
	
	
	void showAppCertInfoDialog(final String entityName)
	{

		if(certificationManager!= null)
		{
			Button refreshData = null;
			
			Bundle ci = null;
			try {
				ci = certificationManager.getApplicationCertificationInformation(entityName);
			} catch (RemoteException e) {
				e.printStackTrace();
			}
			if (ci == null)
			{
				Toast.makeText(this, "Unable to get Certification Information about entity: " + entityName , Toast.LENGTH_LONG).show();
				return;
			}
		
			AlertDialog.Builder builder = new AlertDialog.Builder(this);
			builder.setTitle("Application Certificate Information - " + entityName);
			LinearLayout layout = new LinearLayout(this);
			layout.setOrientation(LinearLayout.VERTICAL);
			
			final SuccesView certifiedSV = new SuccesView(this,"Certified",ci.getBoolean(Defs.CertificateInformation.CERTIFIED));
			layout.addView(certifiedSV);
			
			final TextViewString restrictedUse = new TextViewString(this, "Locales certified for restricted use", ci.getString(Defs.CertificateInformation.RESTRICTED));
		    layout.addView(restrictedUse);
		    
		    final TextViewString nonRestrictedUse = new TextViewString(this, "Locales certified for non restricted use",ci.getString(Defs.CertificateInformation.NONRESTRICTED));
			layout.addView(nonRestrictedUse);
			
			final LastExecutedView timestampLastUpdated = new LastExecutedView(this);
			layout.addView(timestampLastUpdated);
			
			refreshData = new Button(this);
			refreshData.setText("Get application certfication information");
			refreshData.setOnClickListener(new OnClickListener() {
				@Override
				public void onClick(View arg0) {
					Bundle ci = null;
					try {
						ci = certificationManager.getApplicationCertificationInformation(entityName);
					} catch (RemoteException e) {
						e.printStackTrace();
					}
					if (ci == null)
					{
						return;
					}
					certifiedSV.setValue(ci.getBoolean(Defs.CertificateInformation.CERTIFIED));
					restrictedUse.setValue(ci.getString(Defs.CertificateInformation.RESTRICTED));
					nonRestrictedUse.setValue(ci.getString(Defs.CertificateInformation.NONRESTRICTED));
					timestampLastUpdated.setNow();
				}
			});
			layout.addView(refreshData);
			
			timestampLastUpdated.setNow();
			builder.setView(layout);

			
			

			AlertDialog dialog = builder.create();
			dialog.show();
		}
	}

}
